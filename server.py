"""
sonder-runtime MCP server
---------------------
Bridges MCP clients to a local Ollama instance on operator-controlled hardware.

Design goals:
  * The caller decides WHEN to offload, so local compute is idle until used.
  * Tiered models can share one stable alias; bounded keep_alive limits memory use.
  * Zero third-party HTTP deps (stdlib urllib) -> only `mcp` is required.

Tiers (escalation ladder, cheapest first):
  LOCAL BASE (private, free, offline; always bound):
    fast/code/general -> sonder:latest (hardware-sized by setup, operator-owned)
  LOCAL SPECIALIST (capability-routed; either may be left unset by the operator,
                    in which case that work degrades to a base tier):
    reasoning/vision -> unbound until configured with installed models
  CLOUD  (Ollama-hosted, huge, metered; prompt leaves this machine):
    cloud-code/cloud-general -> configured hosted defaults (no local memory cost)
"""

import collections
import contextlib
import hmac
import importlib
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import sonder_runtime.adapters.task_store as task_state_adapter
import sonder_runtime.application.tasks.use_cases as task_use_cases
import sonder_runtime.adapters.eval_history_reader as eval_history_adapter
import sonder_runtime.application.evaluation_history.use_cases as eval_history_use_cases
import sonder_runtime.adapters.memory_store as memory_store
import orchestrator
import retriever
import reward
import reflection
import embeddings
import personas
import summarizer
import code_runner
import isolated_runner
import live_reload
import system_profile
import emotion_vectors
import preference_learning
from sonder_runtime.adapters import process_liveness
import web_tools
import local_service_probe as local_probe
import web_intents
import self_heal
import grounding
import sonder_paths
import harness_tools
import memory_quality
import learning_health
import domain_grounding
import master_orchestrator
import execution_status
import ollama_lifecycle
import admin_auth
import codegen_loop
import file_ops
import data_query as data_query_module
import json_patch_tool
import json_schema_verifier
import text_patch as text_patch_ops
import workspace_compare as workspace_compare_module
import log_inspect as log_inspect_module
import data_convert as data_convert_module
import sqlite_mutate as sqlite_mutate_module
import symbol_index
import project_detect as project_detector
import git_history
import content_digest
import archive_tools
import archive_create as archive_create_tool
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
import dependency_inventory as dependency_inventory_tool
import creative_router
import intents
import runtime_policy
import npu_contract
import npu_service
import calibration
import command_catalog
import grounded_outcomes
import permission_modes
import reloadable_mcp
import autopilot_store
import autopilot_controller
from model_transport import ModelCallError
import context_overflow
import ollama_endpoint
import sonder_speculation
import consult as consult_flow
import code_improve
import tier_router
import project_scaffold
import environment_probe
import sonder_hardware
import sonder_logging
import tool_capabilities
import git_tools
import sonder_runtime.adapters.evaluation_history_store as eval_history
import artifact_risk as artifact_risk_module
import artifact_fetch as artifact_fetch_module
import process_risk as process_risk_module
import unsafe_lab

BASE = ollama_endpoint.normalize()
OLLAMA_HOST = urllib.parse.urlparse(BASE).netloc
# Bind-all Ollama server addresses are canonicalized to a numeric loopback
# destination for this client without mutating the server process environment.
# How long a model stays in VRAM after its last call. Short = frees GPU quickly.
KEEP_ALIVE = os.environ.get("SONDER_KEEP_ALIVE", "2m")
TIMEOUT = int(os.environ.get("SONDER_TIMEOUT", "300"))
SONDER_STABLE_ALIAS = "sonder:latest"
LOCAL_CODE_MODEL = os.environ.get("SONDER_CODE_LOCAL", SONDER_STABLE_ALIAS)
DEFAULT_CLOUD_CODE_MODEL = "kimi-k2.7-code:cloud"
DEFAULT_CLOUD_GENERAL_MODEL = "glm-5.2:cloud"
CLOUD_EXTRA_USAGE_FALLBACK_MODEL = "kimi-k2.7-code:cloud"

# Hosted cloud models Ollama has permanently retired (HTTP 410 at request time).
# A machine-wide SONDER_CLOUD_* override set before a retirement must not keep
# resurrecting the dead model forever -- route it to today's default instead of
# failing every offload call until someone notices and edits their env by hand.
RETIRED_CLOUD_MODELS = frozenset(
    {
        "qwen3-coder:480b-cloud",  # retired 2026-07-15
    }
)


def _live_cloud_model(configured, default):
    lowered = str(configured or "").strip().lower()
    if not lowered or lowered in RETIRED_CLOUD_MODELS:
        return default
    return configured


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
        # Omit num_gpu unless the operator explicitly pins it. Ollama can then
        # select CPU, Metal, ROCm, CUDA, Vulkan, or another supported backend
        # from live host capabilities instead of inheriting an NVIDIA-shaped
        # default from the maintainer's workstation.
        "num_gpu": _env_int_option("SONDER_NUM_GPU"),
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
    "fast": os.environ.get("SONDER_FAST", SONDER_STABLE_ALIAS),
    "code": os.environ.get("SONDER_CODE", SONDER_STABLE_ALIAS),
    "general": os.environ.get("SONDER_GENERAL", SONDER_STABLE_ALIAS),
    # Specialist local tiers the capability router prefers for reasoning and
    # vision work. `_refresh_runtime_policy` drops either one when the shared
    # policy leaves it unbound, so an unset tier is simply not offered and
    # routing degrades to a base tier exactly as it did before they existed.
    "reasoning": os.environ.get("SONDER_REASONING", ""),
    "vision": os.environ.get("SONDER_VISION", ""),
    "cloud-code": _live_cloud_model(
        os.environ.get("SONDER_CLOUD_CODE"), DEFAULT_CLOUD_CODE_MODEL
    ),
    "cloud-general": _live_cloud_model(
        os.environ.get("SONDER_CLOUD_GENERAL"), DEFAULT_CLOUD_GENERAL_MODEL
    ),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}
LOCAL_TIERS = tuple(k for k in TIERS if k not in CLOUD_TIERS)


def _refresh_live_cloud_tiers():
    """Migrate import-time cloud defaults after an atomic source reload.

    Live reload swaps function implementations but deliberately preserves the
    module's process state.  Detect only the exact old default pair, so a fresh
    process with an explicit gpt-oss override remains an intentional override.
    """
    preserve_legacy = os.environ.get(
        "SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if (
        not preserve_legacy
        and TIERS.get("cloud-general") == "gpt-oss:120b-cloud"
    ):
        TIERS["cloud-general"] = "glm-5.2:cloud"


def cloud_allowed():
    return os.environ.get("SONDER_ALLOW_CLOUD", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def available_tiers(include_disabled=False):
    _refresh_live_cloud_tiers()
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


def reasoning_exposure_enabled() -> bool:
    """Whether this deployment surfaces model reasoning to callers.

    Off by default. Ollama returns a reasoning model's thought in
    ``message.thinking``, separate from the answer, but only when the request
    asks for it -- so leaving this off means we never even request it.

    This does not weaken ``admin_private_chain_of_thought``: that refuses
    arbitrary inspection of hidden reasoning, which stays refused. This exposes
    only the reasoning a model emitted for the turn the caller just asked for,
    through a channel the model emits deliberately and separately from its
    answer, and only where the operator has turned it on.
    """
    return os.environ.get("SONDER_EXPOSE_REASONING", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


# Which local models are known to reason. Learned from responses that carry
# message.thinking -- never from a speculative /api/show probe, which would put
# an extra round trip on every model's first request. See
# _remember_thinking_model / _known_thinking_model.
_THINKING_CAPABILITY_CACHE = {}
_THINKING_CAPABILITY_LOCK = threading.Lock()


def _apply_cloud_thinking_policy(payload, model, *, compact=False):
    """Apply hosted-model thinking controls without changing custom models.

    Tool-using agent turns need only a small JSON decision.  Keep their
    reasoning bounded so a hosted model cannot consume the whole prediction
    budget before returning that decision.  Ordinary offloads retain the
    quality-oriented policy below.
    """
    name = str(model or "").strip().casefold()
    if name.startswith("kimi-k3:"):
        # K3 is a native-thinking model; do not assume its hosted endpoint
        # supports disabling thought. Request it explicitly so hosted defaults
        # cannot drift. Compact agent mode keeps the caller's bounded budget;
        # ordinary offloads retain headroom for thinking plus final content.
        payload["think"] = True
        if not compact:
            _ensure_cloud_prediction_budget(payload)
    elif name.startswith("glm-5.2:"):
        # GLM-5.2 accepts an explicit false value.  Even its "low" reasoning
        # mode can consume the entire shared prediction budget without
        # emitting the tiny JSON/native tool decision an agent turn needs.
        # Ordinary offloads retain the quality-oriented high setting.
        payload["think"] = False if compact else "high"
        if not compact:
            _ensure_cloud_prediction_budget(payload)
    elif name.startswith("kimi-k2.7-code:"):
        # Code review benefits materially from the model's native reasoning
        # mode; the hosted API returns final content separately, so callers do
        # not receive or depend on the private thinking stream.
        payload["think"] = False if compact else True
        if not compact:
            _ensure_cloud_prediction_budget(payload)
    elif name.startswith("gpt-oss:"):
        payload["think"] = "low"


def _ensure_cloud_prediction_budget(payload, minimum=4096):
    """Leave enough shared output budget for thinking plus final content."""
    options = payload.get("options")
    if not isinstance(options, dict):
        return
    requested = options.get("num_predict")
    if isinstance(requested, int) and 0 < requested < minimum:
        options = dict(options)
        options["num_predict"] = minimum
        payload["options"] = options


LOCAL_THINKING_MIN_NUM_PREDICT = 2048


def _remember_thinking_model(model):
    """Record, from a response that carried thinking, that this model reasons."""
    name = str(model or "").strip()
    if not name:
        return
    with _THINKING_CAPABILITY_LOCK:
        _THINKING_CAPABILITY_CACHE[name] = True


def _known_thinking_model(model) -> bool:
    """Whether we already know this model reasons. Never performs I/O."""
    with _THINKING_CAPABILITY_LOCK:
        return _THINKING_CAPABILITY_CACHE.get(str(model or "").strip(), False)


def _thinking_exhausted_budget(out, message) -> bool:
    """Did the model spend its whole output budget thinking, leaving no answer?

    The signature is exact: thinking present, content absent, and Ollama
    reporting it stopped on length rather than finishing.
    """
    if not isinstance(message, dict):
        return False
    thinking = message.get("thinking")
    if not isinstance(thinking, str) or not thinking.strip():
        return False
    done_reason = out.get("done_reason") if isinstance(out, dict) else None
    return str(done_reason or "").strip().casefold() == "length"


def _with_local_thinking_budget(payload, minimum=LOCAL_THINKING_MIN_NUM_PREDICT):
    """Return ``payload`` with room for a local model's thinking plus its answer.

    The local mirror of _ensure_cloud_prediction_budget. Returns a copy so a
    caller's dict is never mutated; an unset or already-generous num_predict is
    left alone, and 0/-1 (unlimited) is not a small budget.
    """
    options = payload.get("options")
    if not isinstance(options, dict):
        return dict(payload)
    requested = options.get("num_predict")
    if not isinstance(requested, int) or requested <= 0 or requested >= minimum:
        return dict(payload)
    payload = dict(payload)
    payload["options"] = dict(options, num_predict=minimum)
    return payload


def _cloud_extra_usage_fallback(model, error):
    """Return the plan-covered Kimi fallback for an unfunded K3 request.

    Ollama currently bills Kimi K3 only against the separate extra-usage
    balance, even for Pro/Max accounts.  A 402 is therefore a deterministic
    model-availability decision, not a transient transport failure.  Honor an
    explicit K3 selection, but let opted-in cloud work consume the account's
    ordinary resettable allowance through K2.7 when that balance is empty.
    """
    if not isinstance(error, ModelCallError) or error.status != 402:
        return None
    if not str(model or "").strip().casefold().startswith("kimi-k3:"):
        return None
    if str(model).strip().casefold() == CLOUD_EXTRA_USAGE_FALLBACK_MODEL.casefold():
        return None
    return CLOUD_EXTRA_USAGE_FALLBACK_MODEL


def _chat_request_with_cloud_fallback(
    payload, *, model, timeout=None, cancel_check=None,
    accept_native_tool_calls=False, compact_cloud_reasoning=False,
):
    """Make one cloud request, falling back exactly once on K3 HTTP 402."""
    try:
        out, content = _chat_request(
            payload,
            model=model,
            cloud=True,
            timeout=timeout,
            cancel_check=cancel_check,
            accept_native_tool_calls=accept_native_tool_calls,
            idempotent=True,
        )
        return out, content, model
    except ModelCallError as error:
        fallback = _cloud_extra_usage_fallback(model, error)
        if fallback is None:
            raise

    fallback_payload = dict(payload)
    fallback_payload["model"] = fallback
    fallback_payload.pop("think", None)
    _apply_cloud_thinking_policy(
        fallback_payload, fallback, compact=compact_cloud_reasoning,
    )
    out, content = _chat_request(
        fallback_payload,
        model=fallback,
        cloud=True,
        timeout=timeout,
        cancel_check=cancel_check,
        accept_native_tool_calls=accept_native_tool_calls,
        idempotent=True,
    )
    return out, content, fallback


if _is_cloud_model_name(TIERS["code"]):
    TIERS["code"] = LOCAL_CODE_MODEL


_RUNTIME_POLICY = {}


def _refresh_runtime_policy(create=True):
    """Apply the shared local-only policy without touching cloud configuration."""
    global _RUNTIME_POLICY
    policy = runtime_policy.load(create=create)
    for tier in runtime_policy.LOCAL_TIERS:
        model = str(policy["local_models"].get(tier) or "").strip()
        if model:
            TIERS[tier] = model
        else:
            # An optional tier the operator left unset must not be offered at
            # all: dropping it keeps `_serve_target`, `available_tiers()` and
            # `/v1/models` honest, and the capability router sees it as
            # unavailable and degrades to a base tier.
            TIERS.pop(tier, None)
    _RUNTIME_POLICY = policy
    return policy


def _configured_local_tiers():
    """Local tiers the shared policy currently binds to a model."""
    bound = runtime_policy.bound_tiers(
        _RUNTIME_POLICY or _refresh_runtime_policy(create=True)
    )
    return tuple(tier for tier in bound if tier in TIERS)


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
# Sessioned calls use the context policy selected for the live model and keep the
# last MAX_TURNS turns live; older turns are rolled into a summary.
SESSION_NUM_CTX = context_policy.default_requested()
MAX_TURNS = int(os.environ.get("SONDER_MAX_TURNS", "12"))

_DB_PATH = sonder_paths.memory_db_path()

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")
_CAMPAIGN_LEARN_LOCK = threading.Lock()
_AUTOPILOT_THREADS_LOCK = threading.RLock()
_AUTOPILOT_THREADS = {}
_SESSION_TURN_LOCKS_LOCK = threading.Lock()
_SESSION_TURN_LOCKS = {}
_SESSION_TURN_CLAIM_WAIT_SECONDS = max(
    0, min(30, _env_int_option("SONDER_SESSION_CLAIM_WAIT_SECONDS", 5) or 0)
)

LIVE_RELOAD_MODULES = [
    "sonder_runtime.adapters.task_store",
    "sonder_runtime.application.tasks.use_cases",
    "sonder_runtime.adapters.eval_history_reader",
    "sonder_runtime.application.evaluation_history.use_cases",
    "sonder_runtime.adapters.memory_store",
    "process_liveness",
    "orchestrator",
    "retriever",
    "reward",
    "reflection",
    "embeddings",
    "ollama_endpoint",
    "personas",
    "sonder_runtime.adapters.recall",
    "summarizer",
    "code_runner",
    "isolated_runner",
    "system_profile",
    "emotion_vectors",
    "preference_learning",
    "workflow_store",
    "web_tools",
    "local_service_probe",
    "web_intents",
    "self_heal",
    "memory_quality",
    "learning_health",
    "sonder_runtime.adapters.evaluation_history_store",
    "domain_grounding",
    "fleet_provenance",
    "master_orchestrator",
    "ollama_lifecycle",
    "admin_auth",
    "file_ops",
    "data_query",
    "json_patch_tool",
    "dependency_inventory",
    "sqlite_mutate",
    "symbol_index",
    "project_detect",
    "git_history",
    "git_tools",
    "content_digest",
    "archive_tools",
    "archive_create",
    "text_patch",
    "workspace_compare",
    "log_inspect",
    "data_convert",
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
    "sonder_hardware",
    "tool_capabilities",
    # NPU accelerator host modules reload in dependency order; the broker and
    # service keep live worker/process state behind reload guards.
    "npu_contract",
    "npu_manifest",
    "npu_providers",
    "npu_broker",
    "npu_service",
    # The controller is stateless and safe to refresh between callback calls.
    # autopilot_store intentionally stays loaded because it exclusively owns a
    # process-safe SQLite schema and may be serving background worker threads.
    "autopilot_controller",
    "pdf_risk",
    "artifact_risk",
    "process_risk",
]

def _prime_live_reload_modules():
    """Seed helper baselines across an atomic server-module upgrade.

    ``server.py`` can be refreshed while the process still holds the previous
    ``live_reload`` module object.  Feature-detect the new API and, when needed,
    reload that small helper before using it so an otherwise-valid atomic
    refresh cannot fail with ``AttributeError`` at import time.
    """
    global live_reload
    prime = getattr(live_reload, "prime_modules", None)
    if not callable(prime):
        try:
            live_reload = importlib.reload(live_reload)
        except Exception:
            return False
        prime = getattr(live_reload, "prime_modules", None)
    if not callable(prime):
        return False
    prime(LIVE_RELOAD_MODULES)
    return True


# Seed helper source state at process startup.  Otherwise an edit made before
# the first tool call can be recorded as the baseline while the imported module
# object still contains the older code.
_prime_live_reload_modules()


def _maybe_live_reload():
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    for name, module in modules.items():
        if name == "sonder_runtime.adapters.recall":
            # Recall resolves its migrated adapter lazily through the
            # application gateway. Keep the root compatibility alias on the
            # same live module object without restoring a production import.
            sys.modules[name] = module
            if "recall" in sys.modules:
                sys.modules["recall"] = module
            continue
        if name == "sonder_runtime.adapters.task_store":
            globals()["task_state_adapter"] = module
            continue
        if name == "sonder_runtime.application.tasks.use_cases":
            globals()["task_use_cases"] = module
            continue
        if name == "sonder_runtime.adapters.eval_history_reader":
            globals()["eval_history_adapter"] = module
            continue
        if name == "sonder_runtime.application.evaluation_history.use_cases":
            globals()["eval_history_use_cases"] = module
            continue
        if name == "sonder_runtime.adapters.memory_store":
            globals()["memory_store"] = module
            continue
        if name == "local_service_probe":
            globals()["local_probe"] = module
            continue
        if name == "dependency_inventory":
            globals()["dependency_inventory_tool"] = module
            continue
        if name == "project_detect":
            globals()["project_detector"] = module
            continue
        if name == "data_query":
            globals()["data_query_module"] = module
            continue
        if name == "text_patch":
            globals()["text_patch_ops"] = module
            continue
        if name == "workspace_compare":
            globals()["workspace_compare_module"] = module
            continue
        if name == "log_inspect":
            globals()["log_inspect_module"] = module
            continue
        if name == "data_convert":
            globals()["data_convert_module"] = module
            continue
        if name == "archive_create":
            globals()["archive_create_tool"] = module
            continue
        if name == "workflow_store":
            # The workflow adapter resolves this watched legacy module lazily.
            # Do not recreate a direct server dependency during live reload.
            continue
        if name == "artifact_risk":
            globals()["artifact_risk_module"] = module
            continue
        if name == "process_risk":
            globals()["process_risk_module"] = module
            continue
        if name == "sqlite_mutate":
            globals()["sqlite_mutate_module"] = module
            continue
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


def _deployment_authenticates_callers() -> bool:
    """True when this runtime can serve more than one identity.

    Mirrors sonder_serve's auth-mode resolution without importing it (that
    would be circular). Any of these means callers are distinguishable, so a
    tool returning one caller's data to another is a real disclosure:
    SONDER_AUTH_MODE set at all, an API key configured, or accounts required.
    Absent all three the deployment is `local-open` -- a single operator on
    loopback, where there is no second party to protect.
    """
    if os.environ.get("SONDER_AUTH_MODE", "").strip():
        return True
    if os.environ.get("SONDER_API_KEY", "").strip():
        return True
    return str(os.environ.get("SONDER_REQUIRE_ACCOUNT", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _developer_gate(tool_name: str, token: str, started):
    """Refusal text when the caller may not read another caller's data, else None.

    Deliberately NOT `if token:` -- that shape checks the token only when one
    happens to be supplied, so omitting it skips the check entirely. It reads
    like a gate and fails open. On any deployment that authenticates callers a
    developer token is required; unauthenticated ones are refused rather than
    waved through.
    """
    if not _deployment_authenticates_callers():
        return None
    account = _admin_account_from_token(token) if token else None
    ok, msg = admin_auth.require(account, "developer")
    if ok:
        return None
    _record_direct_tool(tool_name, {}, ok=False, started=started)
    return "refused: %s." % msg


_TURN_TRACES = collections.deque(maxlen=8)


def _capture_turn(model, tier, trace_ctx, prompt, response, iid=None):
    """Keep the last few turns' pipeline state so a turn can be debugged after it.

    ``/trace on`` already prints all of this -- the assembled prompt, the
    lessons retrieved, the tier -- but only for turns run *after* you thought
    to enable it. That is the wrong way round for debugging: you want the
    trace for the turn that already surprised you, and reproducing it is
    exactly what is hard when the behaviour is intermittent.

    ``_answer`` returns ``trace_ctx`` on every turn regardless of the flag, so
    this state is built and then thrown away. Keeping a bounded ring of it in
    memory costs nothing and makes ``turn_inspect`` retrospective. Nothing is
    written to disk; the buffer dies with the process.
    """
    if not isinstance(trace_ctx, dict):
        return
    try:
        _TURN_TRACES.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(model or ""),
            "tier": str(tier or ""),
            "interaction_id": str(iid or ""),
            "prompt": str(prompt or "")[:4000],
            "augmented_prompt": str(trace_ctx.get("augmented_prompt") or "")[:16000],
            "lessons": [str(x)[:400] for x in (trace_ctx.get("lessons") or [])][:20],
            "response_head": str(response or "")[:2000],
        })
    except Exception:
        # Debug bookkeeping must never break the answer path it observes.
        pass


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
        payload = _get("/api/tags")
    except Exception:
        payload = {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            name = str(model.get("name") or model.get("model") or "").strip()
            if name.casefold() == SONDER_STABLE_ALIAS:
                return SONDER_STABLE_ALIAS
    return None if strict else LOCAL_CODE_MODEL


def _make_generate(
    model, system, temperature, num_predict, num_ctx, cloud=False, timeout=None,
    cancel_check=None, accept_native_tool_calls=False,
    compact_cloud_reasoning=False, schema=None,
):
    """Build a generate(prompt, history) closure for `model`.

    cloud=True targets an Ollama-hosted model: keep_alive and num_ctx are omitted
    (they're VRAM/local-context knobs the remote tier doesn't take), matching how the
    non-learning cloud path posts.  The opt-in agent flags accept one canonical
    native tool call and keep hosted reasoning compact for a small JSON contract.

    `schema` is a JSON Schema object handed to Ollama as the decoder-side
    ``format`` constraint. It is omitted from the payload entirely when None, so
    an unconstrained call posts exactly the bytes it always did. Constraining
    the decoder is not the same as verifying the result -- see
    `_require_schema_match`, which callers apply to the returned text.
    """
    cloud = bool(cloud or _is_cloud_model_name(model))

    def gen(prompt, history=None):
        gen.last_usage = {}
        gen.last_response_meta = {}
        usage = {}
        started = time.time()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        prediction_limit = num_predict
        override = getattr(gen, "num_predict_override", None)
        if isinstance(override, int) and not isinstance(override, bool):
            prediction_limit = max(1, min(num_predict, override))
        if cloud:
            options = {"temperature": temperature, "num_predict": prediction_limit}
        else:
            options = _local_model_options(temperature, prediction_limit, num_ctx)
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if schema is not None:
            payload["format"] = schema
        if cloud:
            _apply_cloud_thinking_policy(
                payload, model, compact=compact_cloud_reasoning,
            )
        else:
            payload["keep_alive"] = KEEP_ALIVE
        ok = False
        content = ""
        used_model = model
        try:
            if cloud:
                out, content, used_model = _chat_request_with_cloud_fallback(
                    payload,
                    model=model,
                    timeout=timeout,
                    cancel_check=cancel_check,
                    accept_native_tool_calls=accept_native_tool_calls,
                    compact_cloud_reasoning=compact_cloud_reasoning,
                )
            else:
                out, content = _chat_request(
                    payload,
                    model=model,
                    cloud=False,
                    timeout=timeout,
                    cancel_check=cancel_check,
                    accept_native_tool_calls=accept_native_tool_calls,
                    idempotent=True,
                )
            tokens_in = _model_usage_count(out.get("prompt_eval_count"))
            tokens_out = _model_usage_count(out.get("eval_count"))
            source = "ollama" if tokens_in is not None or tokens_out is not None else "estimated"
            if tokens_in is None:
                tokens_in = sum(_rough_token_count(m.get("content", "")) for m in messages)
            if tokens_out is None:
                tokens_out = _rough_token_count(content)
            usage = {
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "token_source": source,
            }
            gen.last_usage = dict(usage)
            # Keep only the backend's bounded, non-content inference metadata.
            # Gateways can expose measured phase timing without retaining prompts,
            # responses, or arbitrary provider fields.
            gen.last_response_meta = {
                "done_reason": str(out.get("done_reason") or "").strip().casefold(),
                **{
                    key: out.get(key)
                    for key in (
                        "total_duration", "load_duration",
                        "prompt_eval_count", "prompt_eval_duration",
                        "eval_count", "eval_duration",
                        "load_state", "cold_start",
                    )
                    if key in out
                },
            }
            ok = True
        finally:
            activity_tracker.record_model_call(
                model=used_model,
                prompt_chars=len(prompt or ""),
                history_messages=len(history or []),
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                token_source=usage.get("token_source", ""),
                request_preview=prompt,
                response_preview=content if ok else None,
                ok=ok,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        return content
    gen.last_usage = {}
    gen.last_response_meta = {}
    gen.num_predict_override = None
    return gen


def _no_retrieve(conn, task):
    """Retrieve hook that injects nothing — used for 'teacher' (clean) generation so a
    strong model answers at full strength without local-lesson augmentation, while its
    output is still captured for grounding + distillation."""
    return []


def _generate_text(prompt, tier="fast", system="", temperature=0.2,
                   num_predict=256, num_ctx=2048, timeout=None):
    _refresh_live_cloud_tiers()
    model = TIERS.get(tier, TIERS["fast"])
    return _make_generate(
        model, system, temperature, num_predict, num_ctx, timeout=timeout,
    )(prompt)


_APP_GRAPH = None
_APP_GRAPH_LOCK = threading.Lock()


def _application():
    """Lazily build the SPEC-3 composition-root graph (no import-time cost)."""
    global _APP_GRAPH
    with _APP_GRAPH_LOCK:
        if _APP_GRAPH is None:
            from sonder_runtime.bootstrap import app as _bootstrap_app
            _APP_GRAPH = _bootstrap_app.build_application(
                preference_connection_factory=_open_db,
                preference_module_provider=lambda: preference_learning,
            )
        return _APP_GRAPH


def _gateway_generate_text(prompt, tier="fast", system="", temperature=0.2,
                           num_predict=256, num_ctx=None, timeout=None):
    """offload_fn routed through the SPEC-3 ChatService over the ModelGateway.

    The port enforces the operation-context cloud-consent gate and returns
    domain-typed errors; this edge translates them back to ModelCallError
    (a urllib.error.URLError subclass) so existing callers that catch
    URLError — session summarization/titling — keep their exact behavior.
    An explicit num_ctx is forwarded through the port; when omitted the
    gateway resolves the native session context via _make_generate.
    """
    from sonder_runtime.application.chat.handle_chat import ChatCommand
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.domain.common import errors as _errors

    context = local_owner_context(
        correlation_id="offload-%s" % os.urandom(4).hex(),
        source="system",
        cloud_allowed=cloud_allowed(),
        remote_ollama_allowed=not ollama_endpoint.is_loopback(BASE),
        timeout_seconds=float(timeout) if timeout else None,
    )
    try:
        result = _application().chat.complete(
            ChatCommand(
                content=prompt, tier=tier, system=system,
                temperature=temperature, num_predict=num_predict,
                num_ctx=num_ctx,
            ),
            context,
        )
    except _errors.SonderError as exc:
        # Translate the domain taxonomy back to the legacy transport error
        # at the adapter edge so callers' URLError handling is unchanged.
        kind = {
            "DEADLINE_EXCEEDED": "timeout",
            "CANCELLED": "cancelled",
            "DEPENDENCY_UNAVAILABLE": "request",
            "FORBIDDEN": "configuration",
            "INVALID_INPUT": "configuration",
        }.get(getattr(exc, "code", ""), "request")
        raise ModelCallError(kind, str(exc)) from exc
    return result.response_text


def _internal_generate_for_route(model, cloud):
    """Return the generator subordinate steps must use for one request route.

    Local requests intentionally retain the cheap ``fast`` title/summary behavior.
    An explicit cloud route is cloud-only, however: subordinate helpers may request
    ``tier="fast"`` as a cost hint, but must inherit the selected cloud model instead
    of silently loading a local model between cloud calls.
    """
    if not cloud:
        return _generate_text
    if not str(model or "").strip():
        raise ModelCallError(
            "configuration",
            "cloud-only request has no concrete cloud model",
            attempts=0,
            cloud=True,
        )

    def generate(prompt, tier="fast", system="", temperature=0.2,
                 num_predict=256, num_ctx=2048, timeout=None):
        # ``tier`` is deliberately ignored. It is only a subordinate cost hint;
        # the caller's explicit cloud-only route is the stronger constraint.
        generate.last_usage = {}
        gen = _make_generate(
            model, system, temperature, num_predict, num_ctx,
            cloud=True, timeout=timeout,
        )
        response = gen(prompt)
        generate.last_usage = dict(getattr(gen, "last_usage", None) or {})
        return response

    generate.last_usage = {}
    return generate


def _resolve_session(session):
    """"" -> DEFAULT_SESSION (memory on by default); "none" -> None (single turn)."""
    s = (session or "").strip()
    if s == "":
        return DEFAULT_SESSION
    if s.lower() == "none":
        return None
    return s


@contextlib.contextmanager
def _serialized_session_turn(session_id):
    """Serialize remembered turns until their captured response is final."""
    if session_id is None:
        yield
        return
    key = str(session_id)
    with _SESSION_TURN_LOCKS_LOCK:
        entry = _SESSION_TURN_LOCKS.get(key)
        if entry is None:
            entry = {"lock": threading.RLock(), "users": 0}
            _SESSION_TURN_LOCKS[key] = entry
        entry["users"] += 1
    acquired = False
    try:
        entry["lock"].acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry["lock"].release()
        with _SESSION_TURN_LOCKS_LOCK:
            entry["users"] -= 1
            if entry["users"] == 0 and _SESSION_TURN_LOCKS.get(key) is entry:
                _SESSION_TURN_LOCKS.pop(key, None)


def _acquire_persistent_session_turn(session_id):
    """Acquire a DB-backed session claim before reading remembered history."""
    owner_state, owner_identity = process_liveness.probe_process(os.getpid())
    if (
        owner_state != process_liveness.PROCESS_ALIVE
        or not owner_identity
    ):
        return None, "ERROR: session owner identity is unavailable."
    try:
        conn = _open_db()
    except Exception:
        return None, "ERROR: session turn coordination is unavailable."
    claim_token = memory_store.new_id()
    deadline = time.monotonic() + _SESSION_TURN_CLAIM_WAIT_SECONDS
    while True:
        try:
            claimed = memory_store.claim_session_turn(
                conn,
                session_id,
                claim_token,
                owner_pid=os.getpid(),
                owner_identity=owner_identity,
            )
        except Exception:
            conn.close()
            return None, "ERROR: session turn coordination is unavailable."
        if claimed:
            return {
                "conn": conn,
                "session_id": session_id,
                "claim_token": claim_token,
                "owner_pid": os.getpid(),
                "owner_identity": owner_identity,
            }, ""
        if time.monotonic() >= deadline:
            conn.close()
            session_label = str(session_id).replace("\r", " ").replace("\n", " ")[:120]
            return None, (
                "ERROR: session '%s' already has a turn in progress; retry shortly."
                % session_label
            )
        time.sleep(0.05)


def _release_persistent_session_turn(claim):
    if not claim:
        return
    conn = claim["conn"]
    for attempt in range(3):
        try:
            memory_store.release_session_turn(
                conn, claim["session_id"], claim["claim_token"],
            )
            break
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            if attempt == 2:
                memory_store.abandon_session_turn_claim(
                    claim["session_id"], claim["claim_token"],
                    claim["owner_pid"], claim["owner_identity"],
                )
                return
            time.sleep(0.05)
            try:
                conn = _open_db()
            except Exception:
                continue
    try:
        conn.close()
    except Exception:
        pass


def _resolve_project(project):
    """Same convention as sessions: "" -> DEFAULT_PROJECT, "none" -> None."""
    p = (project or "").strip()
    if p == "":
        return DEFAULT_PROJECT
    if p.lower() == "none":
        return None
    return p


# The model resolved for the request currently being prepared. Set by
# _resolve_model_and_system so the identity block can name the model that
# is actually answering rather than offer a table of tiers to pick from.
_ACTIVE_MODEL_HINT = ""


def _join_system_parts(*parts):
    return "\n\n".join(p for p in parts if p)


def _runtime_identity_block() -> str:
    """Authoritative facts about what is actually serving this request.

    Asked what model it was, the runtime answered from the weights -- and a
    local 7B reported itself as "based on OpenAI's GPT-4 architecture,
    approximately 175 billion parameters, training data to September 2023,
    about 10 tokens per second". Every figure is wrong (measured ~36 tok/s
    here) and the architecture line is another model's identity recalled out of
    training text.

    Which half was wrong is the useful part. The same answer described Sonder's
    own surface -- memory, guarded file and program tools, artifact generation,
    orchestration -- correctly, because those facts were already in the system
    prompt. The model facts were not, so answering became recall, and recall is
    the axis this model class is measured worst on.

    Putting the facts in the prompt stops the question being recall. Built from
    the live TIERS table rather than written into system_profile.md, because a
    fact copied into a document is a second source of truth that drifts the
    moment a tier is repointed. No network call: this runs on every request,
    and an /api/show round trip per request would charge every caller for a
    question few of them ask.
    """
    # Naming THIS request's model, not the tier table. The first version listed
    # all seven tiers and the model answered "my architecture is based on the
    # kimi-k2.7-code:cloud tier" while actually running on sonder:latest -- a
    # menu of names it had no way to choose between, so it picked one. A block
    # meant to remove a guess must not introduce a new thing to guess.
    current = ""
    try:
        current = str(_ACTIVE_MODEL_HINT or "")
    except Exception:
        current = ""
    if not current:
        try:
            current = str(TIERS.get("code") or "")
        except Exception:
            return ""
    if not current:
        return ""
    return (
        "Facts about what is serving this request (authoritative -- use these, "
        "never your own recollection):\n"
        "- The model answering right now is `%s`, an open-weights model served "
        "by Ollama on this machine. You are NOT ChatGPT, GPT-4, Claude, or "
        "Gemini, and you share no architecture or training run with them.\n"
        "- Sonder is the runtime around you (memory, tools, policy, grounding). "
        "Sonder is not a model and has no parameters of its own.\n"
        "- If asked about your architecture, parameter count, training data, "
        "training cutoff, or generation speed, and the answer is not in this "
        "block or in the conversation, say you do not know and point the caller "
        "at `ollama ps` or Sonder's diagnostics. Do NOT guess a number, and do "
        "not infer one from the model's name: a confident wrong figure is worse "
        "than an admission." % current
    )


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
    # An active goal is re-stated every turn so a long objective cannot erode
    # into whatever fits the current turn. Fail-soft: goal bookkeeping must
    # never be able to break a conversation.
    try:
        import goal_store
        goal_block = goal_store.context_block()
    except Exception:
        goal_block = ""
    return _join_system_parts(
        _runtime_identity_block(), profile, emotions, goal_block, effective_system
    )


def _resolve_model_and_system(system, trace, strict, persona):
    """Shared prep for the Sonder Runtime tool and HTTP serve layer.

    Returns (model, effective_system); model is None if the strict alias is missing.
    """
    strict_eff = _STRICT_DEFAULT if strict is None else strict
    model = resolve_sonder_model(strict_eff)
    if model is None:
        return None, None
    global _ACTIVE_MODEL_HINT
    _ACTIVE_MODEL_HINT = model or ""
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
    _refresh_live_cloud_tiers()
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
            for turn in memory_store.session_turns_for_project(
                conn, session_id, project_id,
            ):
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
                "usage: /runtime set code=<local-model> reasoning=<local-model> "
                "workbench=<fast|code|general>"
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
            "  /runtime set reasoning=<model> vision=<model>   (specialist "
            "tiers; assign an empty value to leave one unset)\n"
            "  /runtime set router=<tier> workbench=<tier> autopilot=<tier> "
            "fleet=<tier> review=<tier>\n"
            "  /runtime reset\n"
            "Only installed local models are accepted, and execution lanes route "
            "to fast/code/general only."
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
            prefix = "MCP refresh failed closed: %s" % _safe_mcp_error(
                refreshed["error"]
            )
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
            "  /training adopt-legacy --confirm\n"
            "  /training release-alias --confirm\n"
            "  /training rollback\n"
            "Options: --allow-cpu-offload --max-vram N --max-system-ram N "
            "--context-length N --sequence-length N --batch-size N "
            "--gradient-accumulation N --gpu-index N --resume. "
            "--full-finetune is planning-only; attended start supports QLoRA."
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


def refresh_goal_proposals(scope: str = "") -> dict:
    """Turn host-observed improvement findings into *proposed* goals.

    This is the only place Sonder originates an objective for itself, and it
    is deliberately inert: a proposal is a queued suggestion, nothing more.
    Only an explicit ``/goal adopt <id>`` promotes one, and goal_store
    enforces that with a user-actor check. Nothing here starts autopilot,
    edits anything, or reprioritizes existing work.

    Low-severity findings are skipped so the queue stays worth reading.
    """
    import goal_store

    try:
        data = improvement_report_data()
    except Exception:
        return {"proposed": 0, "skipped": 0, "error": "report unavailable"}
    proposed = skipped = 0
    for issue in (data.get("issues") or []):
        severity = str(issue.get("severity") or "").lower()
        title = str(issue.get("title") or "").strip()
        action = str(issue.get("action") or "").strip()
        if severity == "low" or not title:
            skipped += 1
            continue
        objective = "%s: %s" % (issue.get("area", "runtime"), title)
        criteria = [action] if action else []
        criteria.append("the same finding no longer appears in "
                        "system_improvement_report")
        if goal_store.propose(
            objective, criteria, scope=scope, source="self-observed",
        ):
            proposed += 1
        else:
            skipped += 1
    return {"proposed": proposed, "skipped": skipped, "error": ""}


def _goal_command(arg: str) -> str:
    """User-facing goal bookkeeping.

    Slash commands originate only from the user's own chat input, so this
    layer is authorized to pass actor="user"; goal_store independently
    enforces that closure and adoption can never come from model output.
    """
    import goal_store

    text = str(arg or "show").strip() or "show"
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()

    def _fmt(goal):
        if not goal:
            return "no active goal"
        lines = ["%s [%s] %s" % (goal["id"], goal["status"], goal["objective"])]
        for criterion in goal.get("criteria") or []:
            lines.append("  criterion: %s" % criterion)
        for note in (goal.get("notes") or [])[-5:]:
            lines.append("  note: %s" % note.get("text", ""))
        return "\n".join(lines)

    try:
        if action in ("show", "status"):
            return _fmt(goal_store.get_active())
        if action == "set":
            objective, _, criteria = rest.partition("--criteria")
            if not objective.strip():
                return "usage: /goal set <objective> [--criteria a; b; c]"
            goal = goal_store.set_goal(
                objective.strip(), criteria.strip(), origin="user",
            )
            return "goal set\n" + _fmt(goal)
        if action == "note":
            if not rest:
                return "usage: /goal note <progress note>"
            return "noted\n" + _fmt(goal_store.add_note(rest))
        if action in ("done", "complete"):
            goal = goal_store.complete(rest, actor="user")
            return "goal completed: %s" % goal["objective"]
        if action in ("abandon", "drop"):
            goal = goal_store.abandon(rest, actor="user")
            return "goal abandoned: %s" % goal["objective"]
        if action == "refresh":
            result = refresh_goal_proposals()
            if result.get("error"):
                return "ERROR: %s" % result["error"]
            return (
                "goal proposals refreshed: %d new, %d skipped "
                "(review with /goal proposals)"
                % (result["proposed"], result["skipped"])
            )
        if action == "proposals":
            rows = goal_store.proposals()
            if not rows:
                return "no pending goal proposals"
            listing = "\n".join(
                "%s  %s" % (row["id"], row["objective"][:120]) for row in rows
            )
            return listing + (
                "\n(adopt with /goal adopt <id>; dismiss with "
                "/goal decline <id>)"
            )
        if action == "adopt":
            return "adopted\n" + _fmt(goal_store.adopt(rest, actor="user"))
        if action == "decline":
            goal = goal_store.decline(rest, actor="user")
            return "declined proposal %s" % goal["id"]
        if action == "history":
            rows = goal_store.history()
            if not rows:
                return "no closed goals"
            return "\n".join(
                "%s [%s] %s" % (
                    row["id"], row["status"], row["objective"][:100],
                )
                for row in rows
            )
        return (
            "usage: /goal [show|set <objective> [--criteria a; b]|"
            "note <text>|done [reason]|abandon [reason]|refresh|"
            "proposals|adopt <id>|decline <id>|history]"
        )
    except goal_store.GoalError as exc:
        return "ERROR: %s" % exc


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
    if cmd == "/":
        # A bare slash is the "what can I type" gesture.
        return command_catalog.format_matches("")
    if cmd == "/help":
        return command_catalog.help_text(arg.strip())
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
    if cmd in ("/goal", "/goals"):
        return _goal_command(arg)
    if cmd in ("/ensemble",):
        if not arg.strip():
            return "usage: /ensemble <question>   (polls several local tiers, then compounds one answer)"
        return ensemble_answer(arg.strip(), project=_resolve_project(project))
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
    # Every registered tool is catalogued, so /<tool_name> works here as well
    # as in the branches above -- that is what puts the whole surface, not
    # just the hand-written slice, in reach of the console, app, and API. A
    # slash line that names no known tool still returns None and reaches the
    # model unchanged, so ordinary prose beginning with "/" is unaffected.
    try:
        parsed = command_catalog.parse_invocation(text)
    except ValueError as exc:
        return str(exc)
    if parsed:
        tool, kwargs = parsed
        handler = globals().get(tool)
        if callable(handler):
            try:
                return str(handler(**kwargs))
            except TypeError as exc:
                return "%s: %s\n\n%s" % (
                    cmd, exc, command_catalog.help_command(cmd),
                )
    return None


def _canonical_learn_tier(tier_label):
    """Map a recorded tier label to the LEARN_TIERS key that governs it. The local
    learning route is labeled 'sonder' on interactions but is gated by the same 'code'
    switch as offload's local coder, so both flip together."""
    return "code" if tier_label == "sonder" else tier_label


_ALL_PROJECTS = object()


def _session_history_messages(
    conn, session_id, max_turns, project=_ALL_PROJECTS,
    internal_generate=None,
):
    """Build the prior-turn chat messages for a session, summarizing overflow.

    Turns older than the last `max_turns` are folded (once) into either the
    legacy all-project summary or a project-keyed summary. Summarization is
    best-effort: if it fails, we simply send the live turns.
    """
    scoped = project is not _ALL_PROJECTS
    # SPEC-3: session summarization's model call routes through the
    # ModelGateway port by default (consent gate + typed errors), with the
    # legacy generator still injectable for cloud-route inheritance.
    generate_text = (
        _gateway_generate_text if internal_generate is None else internal_generate
    )
    if scoped:
        turns = memory_store.session_turns_for_project(conn, session_id, project)
        sess = memory_store.get_session_project_summary(conn, session_id, project)
    else:
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
                summary = summarizer.summarize(
                    summary, pairs, generate_text,
                )
                if scoped:
                    memory_store.update_session_project_summary(
                        conn, session_id, project, summary, new_overflow[-1]["id"],
                    )
                else:
                    memory_store.update_session_summary(
                        conn, session_id, summary, new_overflow[-1]["id"],
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


def _maybe_title(conn, session_id, first_prompt, internal_generate=None):
    """Give a brand-new session a short title (best-effort, never fatal)."""
    generate_text = (
        _gateway_generate_text if internal_generate is None else internal_generate
    )
    sess = memory_store.get_session(conn, session_id) or {}
    if sess.get("title"):
        return
    try:
        title = summarizer.make_title(
            first_prompt, generate_text,
        )
    except urllib.error.URLError:
        title = (first_prompt or "").strip()[:40]
    memory_store.set_session_title(conn, session_id, title)


def _preference_facts(conn, task, project=None, limit=12):
    """Return only enabled, scope-authorized preferences relevant to ``task``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    limit = min(limit, 12)
    scopes = []
    if isinstance(project, str) and project:
        scopes.extend((str(project), "project:%s" % project))
    scopes.append("global")
    selected = []
    seen_ids = set()
    seen_keys = set()
    # Scan the full bounded retrieval window so high-ranked but inapplicable
    # legacy rows cannot starve a lower-ranked applicable preference.
    candidate_limit = 200
    for scope in scopes:
        for pref in memory_store.preferences_for_scope(
            conn, scope, limit=candidate_limit
        ):
            if not isinstance(pref, dict):
                continue
            text = pref.get("text", "")
            if not isinstance(text, str):
                continue
            identity = pref.get("id")
            if not isinstance(identity, (str, bytes, int, float)):
                identity = (scope, str(pref.get("key")), text)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            if not preference_learning.preference_applies(text, task):
                continue
            key = pref.get("key")
            if not isinstance(key, str) or not key:
                key = preference_learning.preference_key(text)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected.append("User preference: %s" % text)
            if len(selected) >= limit:
                return selected
    return selected


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
    if not embeddings.valid_vector(qv):
        qv = None
    blob = embeddings.to_blob(qv) if qv else None
    embedding_provenance = embeddings.provenance(qv) if qv else {}
    if augment:
        recalls = (
            _application().recall.retrieve(
                conn, prompt, qv=qv, exclude_session=session_id,
                project=project,
                embedding_model=embedding_provenance.get("model"),
                embedding_revision=embedding_provenance.get("revision"),
            )
            if project is not None else []
        )
        facts = _preference_facts(conn, prompt, project=project)
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
            project=project,
            project_explicit=True,
            task_embedding_model=embedding_provenance.get("model"),
            task_embedding_revision=embedding_provenance.get("revision"),
            task_embedding_dim=embedding_provenance.get("dimension"),
        )
        _capture_preferences(
            conn, prompt, source_interaction=iid,
            scope="project:%s" % project if project else "global",
        )
        return resp, iid, tctx
    resp, iid = orchestrator.run_with_learning(
        conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
        recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
        project=project,
        project_explicit=True,
        task_embedding_model=embedding_provenance.get("model"),
        task_embedding_revision=embedding_provenance.get("revision"),
        task_embedding_dim=embedding_provenance.get("dimension"),
    )
    _capture_preferences(
        conn, prompt, source_interaction=iid,
        scope="project:%s" % project if project else "global",
    )
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


def _release_lesson_distillation_claim(
    interaction_id, claim_token, owner_pid, owner_identity, error,
):
    """Best-effort release of an exact distillation claim for a later retry."""
    if not interaction_id or not claim_token:
        return False
    try:
        conn = _open_db()
        try:
            released = memory_store.mark_lesson_distillation_retryable(
                conn, interaction_id, claim_token, error,
            )
            if released:
                return True
            exact_claim = conn.execute(
                "SELECT 1 FROM lesson_distillations WHERE interaction_id=? "
                "AND state=? AND claim_token=? AND owner_pid=? "
                "AND owner_identity=?",
                (
                    interaction_id, memory_store.DISTILLATION_CLAIMED,
                    claim_token, owner_pid, owner_identity,
                ),
            ).fetchone()
            if exact_claim is None:
                return False
        finally:
            conn.close()
    except Exception:
        pass
    return memory_store.abandon_lesson_distillation_claim(
        interaction_id, claim_token, owner_pid, owner_identity,
    )


def _defer_lesson_distillation(
    interaction_id, claim_token, owner_pid, owner_identity, failure,
):
    """Release a failed claim without persisting private exception details."""
    # Persist only a stable error class. Transport exception text can contain
    # endpoints, filesystem paths, or fragments derived from private prompts.
    error = "distillation failed: %s" % type(failure).__name__
    return _release_lesson_distillation_claim(
        interaction_id, claim_token, owner_pid, owner_identity, error,
    )


def _distillation_timeout_seconds():
    """Return the short, independently configurable learning-call budget."""
    value = _env_int_option("SONDER_DISTILLATION_TIMEOUT", 20)
    if value is None:
        value = 20
    return max(1, min(int(value), TIMEOUT))


def _prepare_lesson_candidate_bounded(interaction, signal):
    """Generate one lesson within a bounded total model/embedding budget."""
    budget = _distillation_timeout_seconds()
    deadline = time.monotonic() + budget

    def remaining_seconds():
        remaining = deadline - time.monotonic()
        return max(1, min(budget, int(remaining) + 1))

    def generate(prompt, **options):
        # reflection.distill passes tier/system/temperature/num_predict;
        # forward them, but this budget's deadline always owns the timeout.
        # SPEC-3: routed through the ModelGateway port; num_ctx pins the
        # small context distillation has always used.
        options.pop("timeout", None)
        options.setdefault("num_ctx", 2048)
        return _gateway_generate_text(
            prompt, timeout=remaining_seconds(), **options
        )

    def embed(text):
        if time.monotonic() >= deadline:
            return None
        return embeddings.embed(text, timeout=remaining_seconds())

    return reflection.prepare_lesson_candidate(
        interaction["task"],
        interaction["response"],
        signal,
        offload_fn=generate,
        embed_fn=embed,
    )


def _record_outcome_and_maybe_distill(interaction_id, signal):
    """Atomically record an outcome and run at most one claimed distillation."""
    claim_token = memory_store.new_id()
    owner_pid = os.getpid()
    owner_state, owner_identity = process_liveness.probe_process(owner_pid)
    if owner_state != process_liveness.PROCESS_ALIVE or not owner_identity:
        owner_pid = 0
        owner_identity = None
    recorded = None
    result = None
    claim_may_exist = False
    try:
        # SPEC-3: the outcome write + distillation claim go through the
        # UnitOfWork-owned MemoryRepository; _DB_PATH keeps the flow on the
        # server's database (tests repoint it) with identical connection
        # semantics.
        with _application().unit_of_work(db_path=_DB_PATH) as uow:
            inter = uow.memory.get_interaction(interaction_id)
            if inter is None:
                return {"found": False}
            score = reward.score(signal)
            claim_may_exist = True
            recorded = uow.memory.record_outcome(
                interaction_id,
                signal,
                score,
                claim_token=claim_token,
                owner_pid=owner_pid,
                owner_identity=owner_identity,
            )
            if not recorded["claimed"]:
                claim_may_exist = False

        result = {
            "found": True,
            "reward": score,
            "outcome_inserted": recorded["outcome_inserted"],
            "distillation_state": recorded["distillation_state"],
            "lesson_id": None,
            "distillation_reason": None,
            "distillation_deferred": (
                recorded["distillation_state"]
                == memory_store.DISTILLATION_RETRYABLE
            ),
        }
        if not recorded["claimed"]:
            return result

        active_model_calls = master_orchestrator.active_model_call_count()
        if active_model_calls:
            released = _release_lesson_distillation_claim(
                interaction_id,
                claim_token,
                owner_pid,
                owner_identity,
                "distillation deferred: active fleet model calls",
            )
            claim_may_exist = not released
            result["distillation_deferred"] = released
            if released:
                result["distillation_state"] = memory_store.DISTILLATION_RETRYABLE
            return result

        candidate = _prepare_lesson_candidate_bounded(inter, signal)
        conn = _open_db()
        try:
            finalized = memory_store.finalize_lesson_distillation(
                conn,
                interaction_id,
                claim_token,
                lambda transaction: reflection.store_prepared_lesson(
                    transaction, interaction_id, candidate,
                ),
            )
            claim_may_exist = False
        finally:
            conn.close()

        result["distillation_state"] = finalized["distillation_state"]
        # The finalizer's reason is the only account of WHY a candidate was
        # refused; dropping it here is what forced a model replay to answer
        # "where is yield lost?". memory_store persists the same normalized
        # value on the ledger row, so the two can never disagree.
        result["distillation_reason"] = memory_store.normalize_distillation_reason(
            finalized["result"]
        )
        result["distillation_deferred"] = False
        if (
            finalized["finalized"]
            and finalized["distillation_state"]
            == memory_store.DISTILLATION_STORED
        ):
            result["lesson_id"] = finalized["lesson_id"]
        return result
    except BaseException as failure:
        released = False
        if claim_may_exist:
            released = _defer_lesson_distillation(
                interaction_id,
                claim_token,
                owner_pid,
                owner_identity,
                failure,
            )
        if not isinstance(failure, Exception):
            raise
        if recorded is None or not recorded.get("claimed"):
            raise
        if result is None:
            result = {
                "found": True,
                "reward": score,
                "outcome_inserted": recorded["outcome_inserted"],
                "distillation_state": recorded["distillation_state"],
                "lesson_id": None,
                "distillation_reason": None,
                "distillation_deferred": False,
            }
        result["distillation_deferred"] = released
        if released:
            result["distillation_state"] = memory_store.DISTILLATION_RETRYABLE
        return result


def _drain_deferred_distillations(limit=16):
    """Retry deferred lesson distillations once the fleet is quiet.

    Campaigns intentionally defer distillation while their own model calls
    hold the GPU, but a retryable job is only reclaimed when another outcome
    lands on the same interaction — which campaign interactions never get.
    This bounded drain closes that loop; duplicate outcomes are storage
    no-ops, so re-recording the original signal is safe.
    """
    if master_orchestrator.active_model_call_count():
        return {"drained": 0, "stored": 0, "deferred": 0}
    try:
        conn = _open_db()
        try:
            pending = memory_store.list_retryable_distillations(conn, limit)
        finally:
            conn.close()
    except Exception:
        return {"drained": 0, "stored": 0, "deferred": 0}
    stored = deferred = 0
    for interaction_id, signal in pending:
        if signal not in reward.VALID_SIGNALS:
            continue
        try:
            result = _record_outcome_and_maybe_distill(interaction_id, signal)
        except Exception:
            continue
        if result.get("lesson_id"):
            stored += 1
        elif result.get("distillation_deferred"):
            deferred += 1
    # `deferred` counts only what stayed deferred inside this LIMIT-bounded
    # batch, which answers "how much of this batch failed" -- not "how big is
    # the backlog". Draining 16 of 500 successfully reported "still deferred 0"
    # with 484 outstanding. Report the real remainder alongside it.
    # Seeding this with `deferred` reinstated the very bug above when the count
    # query lost the race with the campaign's own writers ("database is
    # locked"): the batch number was printed as the backlog, so 484 outstanding
    # jobs reported "backlog remaining 0". An unknown remainder is reported as
    # unknown -- None, never a number that happens to be in scope.
    backlog = None
    try:
        conn = _open_db()
        try:
            backlog = memory_store.count_retryable_distillations(conn)
        finally:
            conn.close()
    except Exception:
        pass
    return {
        "drained": len(pending),
        "stored": stored,
        "deferred": deferred,
        "backlog": backlog,
    }


def _drain_backlog_text(drain):
    """Render the drain's remaining backlog, or say it could not be read."""
    backlog = drain.get("backlog")
    return "unknown (count query failed)" if backlog is None else str(backlog)


def _campaign_headline(
    passed, total, recorded, failed_recorded, pitfall_errors, elapsed,
):
    """Build the campaign's first line - the only line an unattended run keeps.

    scripts/nightly_self_improve.py records _first_line() of each tool result,
    so anything a nightly review must be able to see has to appear here. A
    pitfall-distillation crash reported only in a per-attempt record, or on a
    later summary line, is invisible in exactly the run where nobody is
    watching. The count is appended only when non-zero so a healthy run's
    headline stays byte-identical to what it has always been.
    """
    headline = (
        "campaign generate/compile/execute/record: "
        "%d/%d passed, %d recorded, %d failed-recorded"
        % (passed, total, recorded, failed_recorded)
    )
    if pitfall_errors:
        headline += ", %d pitfall-errors" % pitfall_errors
    return "%s in %.3fs" % (headline, elapsed)


def _record_failure_pitfall(interaction_id, task, response, error):
    """Turn a terminal failure into a durable pitfall lesson.

    Successes distilled lessons; failures recorded a negative reward and were
    then forgotten, so an identical parse error could recur every night
    without the loop ever learning the guard. Pitfalls are additive and
    deduplicated, so they do not touch the success-distillation claim state
    machine. Environmental errors are filtered by the distiller itself and by
    the callers, which only pass model-attributable failures.

    Returns ``(lesson_id, note)``. ``note`` is empty whenever the pipeline ran
    to completion - including when the gates deliberately refused a weak
    lesson - and carries a short diagnostic when distillation raised. The two
    are reported apart because a refusal and a crash both used to return "",
    which makes a pitfall path that breaks during an unattended run look
    exactly like one that had nothing worth learning. Staying best-effort
    still matters more than the note: a broken distiller must never fail the
    campaign attempt that fed it.
    """
    interaction_id = str(interaction_id or "").strip()
    if not interaction_id or not str(error or "").strip():
        return "", ""
    try:
        candidate = reflection.prepare_pitfall_candidate(
            str(task or "")[:4000], str(response or "")[:4000], error,
            # SPEC-3: routed through the ModelGateway port; num_ctx pins the
            # small context distillation has always used.
            offload_fn=lambda prompt, **options: _gateway_generate_text(
                prompt, timeout=_distillation_timeout_seconds(),
                num_ctx=options.pop("num_ctx", 2048), **options
            ),
        )
        if candidate.get("status") != "candidate":
            return "", ""
        conn = _open_db()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                stored = reflection.store_prepared_lesson(
                    conn, interaction_id, candidate,
                )
        finally:
            conn.close()
        return str(stored.get("lesson_id") or ""), ""
    except Exception as exc:
        return "", "pitfall distillation failed: %s: %s" % (
            type(exc).__name__, str(exc)[:120],
        )


def _record_code_gate_failure(interaction_id):
    """Record a negative 'failed' outcome for a reply whose code did not run.

    Best-effort: the auto-negative both keeps broken code out of lesson
    distillation and corrects the outcome-signal skew (previously ~97%
    positive because failures were simply never recorded).

    It still must not raise -- this runs inside a reply path, and losing the
    user's answer to record a statistic would be a worse trade. But it may not
    fail SILENTLY either, and that asymmetry is the point: positives arrive
    through explicit record_outcome calls that surface their errors to the
    caller, while this is the only outcome the runtime records on its own. A
    swallowed exception here therefore drops a negative and nothing else,
    re-inflating the very skew this function exists to correct -- invisibly,
    and in the flattering direction. So the failure is recorded as an activity
    event instead of vanishing."""
    if not interaction_id:
        return
    try:
        _record_outcome_and_maybe_distill(interaction_id, "failed")
    except Exception as exc:
        with contextlib.suppress(Exception):
            activity_tracker.record_event(
                "outcome_record_failed",
                summary="auto-negative for %s was lost: %s" % (
                    str(interaction_id)[:40], str(exc)[:160],
                ),
            )


def _persist_verified_code_repair(
    interaction_id, expected, repaired_response, repair_usage,
):
    """Replace a captured broken response only while its learning state is unchanged."""
    if not interaction_id or not expected or not isinstance(repair_usage, dict):
        return False
    try:
        repair_tokens_in = int(repair_usage["tokens_in"])
        repair_tokens_out = int(repair_usage["tokens_out"])
        original_tokens_in = int(expected.get("tokens_in") or 0)
        original_tokens_out = int(expected.get("tokens_out") or 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if min(
        repair_tokens_in, repair_tokens_out,
        original_tokens_in, original_tokens_out,
    ) < 0:
        return False
    original_source = str(expected.get("token_source") or "").strip().lower()
    repair_source = str(repair_usage.get("token_source") or "").strip().lower()
    if original_source == repair_source == "ollama":
        token_source = "ollama+code-repair"
    elif original_source == repair_source == "estimated":
        token_source = "estimated+code-repair"
    else:
        token_source = "mixed+code-repair"
    try:
        conn = _open_db()
        try:
            return memory_store.replace_interaction_response_cas(
                conn,
                interaction_id,
                expected=expected,
                response=repaired_response,
                tokens_in=original_tokens_in + repair_tokens_in,
                tokens_out=original_tokens_out + repair_tokens_out,
                token_source=token_source,
            )
        finally:
            conn.close()
    except Exception:
        return False


def _apply_code_gate(reply, interaction_id=None, regenerate=None):
    """Compile+smoke-run the reply's runnable Python block before returning it.

    Returns (reply, verified, repaired):
      True  -> the block ran cleanly (reply unchanged, or the repaired reply).
      False -> still failing after one repair round-trip; the reply carries an
               explicit NOT VERIFIED banner and the captured interaction got a
               negative 'failed' outcome.
      None  -> nothing to gate, gate disabled, or inconclusive (an initial
               timeout is not failure: long-running demos/servers are legal).
      repaired is True only when a regenerated reply ran cleanly. A timed-out
      retry never replaces the original response or inherits its interaction ID.
    """
    if not _code_gate_enabled():
        return reply, None, False
    code = _code_gate_target(reply)
    if code is None:
        return reply, None, False
    try:
        result = grounding.run_code_detail(
            code, timeout=_CODE_GATE_TIMEOUT, compile_first=True,
        )
    except Exception:
        return reply, None, False
    if result.get("ok"):
        return reply, True, False
    if result.get("timed_out"):
        return reply, None, False
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
                return repaired, True, True
            if retry.get("timed_out"):
                failure = "%s (repair verification timed out)" % failure
            else:
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


_TRANSIENT_MODEL_HTTP_CODES = frozenset({408, 429, 502, 503, 504})
_MAX_LOCAL_MODEL_RETRIES = 2
_MAX_MODEL_RESPONSE_BYTES = 16 * 1024 * 1024


def _local_model_retries() -> int:
    raw = os.environ.get("SONDER_LOCAL_RETRIES", "1").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(0, min(value, _MAX_LOCAL_MODEL_RETRIES))


def _hosted_overflow_retry_enabled() -> bool:
    """Operator opt-in for compaction retries on hosted/remote model routes.

    Off by default. Even when on it is not sufficient: the calling site must also
    declare the request idempotent, because a hosted retry is metered work that
    may duplicate a side effect.
    """
    return os.environ.get("SONDER_HOSTED_OVERFLOW_RETRY", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _overflow_retry_allowed(*, cloud: bool, remote: bool, idempotent: bool) -> bool:
    """Whether this route may spend one extra attempt on a compacted prompt.

    Loopback Ollama is free and side-effect-free, so it may always take the one
    retry. Anything that leaves the machine - a hosted tier or a remote Ollama -
    keeps the existing "no retries" posture unless the request was explicitly
    declared idempotent *and* the operator opted in.
    """
    if not (cloud or remote):
        return True
    return bool(idempotent) and _hosted_overflow_retry_enabled()


def _embedded_model_error(result) -> str:
    """Error text from a 2xx body that reports failure in-band, else ""."""
    if not isinstance(result, dict):
        return ""
    embedded = result.get("error")
    if not embedded:
        return ""
    return _safe_model_error_detail(embedded)


def _compacted_overflow_payload(payload, verdict):
    """One bounded compaction of `payload` for a classified context overflow.

    Returns a new payload, or None when there is nothing safe to drop. Only the
    message list changes: `options` (and therefore `num_ctx`) is carried through
    untouched, so recovery never silently widens the context window behind the
    context policy's back.
    """
    if not verdict.overflow or not isinstance(payload, dict):
        return None
    compacted = context_overflow.compact_messages(payload.get("messages"))
    if compacted is None:
        return None
    updated = dict(payload)
    updated["messages"] = compacted
    return updated


def _local_retry_delay(attempt: int) -> float:
    raw = os.environ.get("SONDER_LOCAL_RETRY_DELAY_MS", "150").strip()
    try:
        base_ms = float(raw)
    except (TypeError, ValueError):
        base_ms = 150.0
    base_ms = max(0.0, min(base_ms, 1000.0))
    return min(1.0, (base_ms / 1000.0) * (2 ** max(0, attempt - 1)))


def _redact_model_error_value(value, depth: int = 0):
    if depth > 4:
        return "<nested>"
    if isinstance(value, dict):
        redacted = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            lowered = name.lower().replace("-", "_")
            if any(part in lowered for part in (
                "password", "passwd", "secret", "token", "api_key",
                "authorization", "credential",
            )):
                redacted[name] = "<redacted>"
            else:
                redacted[name] = _redact_model_error_value(item, depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_model_error_value(item, depth + 1) for item in list(value)[:64]]
    return value


def _safe_model_error_detail(value, limit: int = 600) -> str:
    structured = isinstance(value, (dict, list, tuple))
    if structured:
        value = json.dumps(
            _redact_model_error_value(value), ensure_ascii=False, sort_keys=True,
        )
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Error bodies should not become a second persistence surface for bearer
    # tokens or API keys accidentally echoed by an upstream proxy.
    if not structured:
        text = re.sub(
            r"(?i)\b(bearer|token|secret|api[-_]?key)\b\s*[:=]?\s*"
            r"(?!(?:limit|count|budget|window|usage|quota|length|context|maximum|minimum)\b)"
            r"\S+",
            r"\1=<redacted>",
            text,
        )
    return text[:limit] or "model request failed"


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    detail = getattr(error, "reason", "") or "HTTP %s" % error.code
    try:
        raw = error.read(4097)
        if raw:
            decoded = raw[:4096].decode("utf-8", errors="replace")
            try:
                parsed = json.loads(decoded)
                if isinstance(parsed, dict) and parsed.get("error"):
                    decoded = parsed["error"]
                elif isinstance(parsed, (dict, list)):
                    decoded = parsed
            except (TypeError, ValueError):
                pass
            detail = decoded
    except Exception:
        pass
    return _safe_model_error_detail(detail)


def _transport_error_detail(error) -> str:
    reason = getattr(error, "reason", error)
    return _safe_model_error_detail(reason)


def _cancel_requested(cancel_check) -> bool:
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        # Cancellation state is a safety gate. If the durable fleet ledger
        # cannot be read, do not authorize another expensive request.
        return True


def _ollama_display() -> str:
    return ollama_endpoint.safe_display(BASE)


def _require_ollama_endpoint(*, cloud: bool = False) -> None:
    error = ollama_endpoint.policy_error(BASE)
    if error:
        raise ModelCallError(
            "configuration", error, attempts=0, cloud=cloud,
        )


def _post_model(
    path: str,
    payload: dict,
    *,
    model: str,
    cloud: bool = False,
    timeout: int | None = None,
    cancel_check=None,
    idempotent: bool = False,
) -> tuple[dict, int]:
    """POST one logical model request with a narrow loopback-only retry policy.

    Cloud calls stay single-attempt to avoid duplicate metered work. Local calls
    retry only transport failures and explicitly transient HTTP statuses, using
    the original timeout as one total monotonic budget. The endpoint, model, and
    payload never change between attempts.

    The single exception is a *classified* context overflow. When the failure
    text itself says the prompt did not fit, this function may spend exactly one
    extra attempt on a compacted prompt - within the same monotonic deadline and
    behind the same cancellation gate as every other attempt. `idempotent` is the
    caller's declaration that repeating this request is safe; it is required (on
    top of an operator opt-in) before a hosted or remote route will take even
    that one retry.
    """
    cloud = bool(cloud or _is_cloud_model_name(model))
    if cloud and not cloud_allowed():
        raise ModelCallError(
            "configuration",
            _cloud_disabled_message().removeprefix("ERROR: "),
            attempts=0,
            cloud=True,
        )
    _require_ollama_endpoint(cloud=cloud)
    remote_endpoint = not ollama_endpoint.is_loopback(BASE)
    request_timeout = _bounded_timeout(timeout)
    deadline = time.monotonic() + request_timeout
    max_attempts = (
        1 if cloud or remote_endpoint else 1 + _local_model_retries()
    )

    # Bumped by exactly one if a classified context overflow earns a compaction
    # retry, so that recovery cannot eat the ordinary transient retry budget.
    compaction_spent = False
    attempt_index = 0
    while attempt_index < max_attempts:
        attempt = attempt_index + 1
        if _cancel_requested(cancel_check):
            raise ModelCallError(
                "cancelled",
                "model call cancelled before another request was sent",
                attempts=attempt_index,
                cloud=cloud,
            )
        remaining = deadline - time.monotonic()
        if attempt > 1 and remaining < 1.0:
            raise ModelCallError(
                "timeout",
                "model retry budget exhausted",
                transient=True,
                attempts=attempt_index,
                cloud=cloud,
            )
        attempt_index = attempt
        failure = None
        embedded_detail = ""
        try:
            if timeout is None and attempt == 1:
                result = _post(path, payload)
            else:
                call_timeout = request_timeout if attempt == 1 else max(1, int(remaining))
                result = _post(path, payload, timeout=call_timeout)
            # Ollama can report a refusal in-band on a 200. That is the same
            # failure surface as an HTTP error body, so it gets classified too
            # rather than being handed upward as an opaque success.
            embedded_detail = "" if compaction_spent else _embedded_model_error(result)
            if not embedded_detail:
                return result, attempt
        except ModelCallError as error:
            raise ModelCallError(
                error.kind,
                error.detail,
                transient=False,
                status=error.status,
                attempts=attempt,
                cloud=cloud,
            ) from error
        except urllib.error.HTTPError as error:
            status = int(getattr(error, "code", 0) or 0)
            transient = status in _TRANSIENT_MODEL_HTTP_CODES
            failure = ModelCallError(
                "http",
                _http_error_detail(error),
                transient=transient,
                status=status,
                attempts=attempt,
                cloud=cloud,
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.IncompleteRead,
        ) as error:
            reason = getattr(error, "reason", error)
            failure = ModelCallError(
                "timeout" if isinstance(reason, TimeoutError) else "transport",
                _transport_error_detail(error),
                transient=True,
                attempts=attempt,
                cloud=cloud,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ModelCallError(
                "protocol",
                "Ollama returned malformed JSON",
                attempts=attempt,
                cloud=cloud,
            ) from error

        if not compaction_spent:
            detail = embedded_detail or failure.detail
            status = None if embedded_detail else failure.status
            verdict = context_overflow.classify(detail, status=status)
            compacted = None
            if verdict.overflow and _overflow_retry_allowed(
                cloud=cloud, remote=remote_endpoint, idempotent=idempotent,
            ):
                compacted = _compacted_overflow_payload(payload, verdict)
            if compacted is not None:
                remaining = deadline - time.monotonic()
                if remaining >= 1.0 and not _cancel_requested(cancel_check):
                    # One extra attempt, inside the same deadline. num_ctx is
                    # deliberately not raised: the prompt shrinks instead.
                    payload = compacted
                    compaction_spent = True
                    max_attempts += 1
                    activity_tracker.record_event(
                        "model_context_compaction",
                        model=str(model or "")[:80],
                        attempt=attempt + 1,
                        reason=verdict.reason,
                        status=verdict.status,
                        control=verdict.control,
                    )
                    continue

        if embedded_detail:
            # Not an overflow we can act on; hand the in-band error upward
            # exactly as before so the caller raises its own typed failure.
            return result, attempt

        if cloud or not failure.transient or attempt >= max_attempts:
            raise failure
        delay = _local_retry_delay(attempt)
        remaining = deadline - time.monotonic()
        if remaining < delay + 1.0:
            raise failure
        activity_tracker.record_event(
            "model_retry",
            model=str(model or "")[:80],
            attempt=attempt + 1,
            max_attempts=max_attempts,
            reason=(
                "http-%s" % failure.status
                if failure.status is not None else failure.kind
            ),
            delay_ms=int(delay * 1000),
        )
        if delay:
            time.sleep(delay)

    raise AssertionError("unreachable model retry state")


_NATIVE_TOOL_ARGUMENTS_MAX_CHARS = 65536
_NATIVE_TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


def _native_tool_call_decision(message):
    """Translate one canonical native tool call into the agent JSON contract.

    The host still applies the normal tool allowlist, path confinement, and
    mutation policy after this translation.  Thinking text and provider-owned
    metadata are intentionally excluded.
    """
    if not isinstance(message, dict):
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
        return None
    tool_call = tool_calls[0]
    function = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not _NATIVE_TOOL_NAME_RE.fullmatch(name):
        return None
    if "arguments" not in function:
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        if len(arguments) > _NATIVE_TOOL_ARGUMENTS_MAX_CHARS:
            return None
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError, RecursionError):
            return None
    if not isinstance(arguments, dict):
        return None
    try:
        encoded_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(encoded_arguments) > _NATIVE_TOOL_ARGUMENTS_MAX_CHARS:
        return None
    try:
        return json.dumps(
            {
                "tool": name,
                "args": arguments,
                "reason": "model native tool call",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def _chat_request(
    payload: dict,
    *,
    model: str,
    cloud: bool = False,
    timeout: int | None = None,
    cancel_check=None,
    accept_native_tool_calls: bool = False,
    idempotent: bool = False,
    _budget_retried: bool = False,
) -> tuple[dict, str]:
    if not cloud and _known_thinking_model(model):
        # A reasoning model spends num_predict on thought BEFORE writing any
        # content, so a tight cap hits done_reason "length" having emitted
        # nothing. Give thinking headroom, as the cloud path does. Cache-only:
        # this must not add a speculative round trip to the hot path.
        payload = _with_local_thinking_budget(payload)
        # Never override an explicit choice; the cloud policy sets `think`
        # itself. Ollama returns message.thinking for these models regardless,
        # so this is belt-and-braces rather than the mechanism.
        if reasoning_exposure_enabled() and "think" not in payload:
            payload["think"] = True
    out, attempts = _post_model(
        "/api/chat",
        payload,
        model=model,
        cloud=cloud,
        timeout=timeout,
        cancel_check=cancel_check,
        idempotent=idempotent,
    )
    if not isinstance(out, dict):
        raise ModelCallError(
            "protocol", "Ollama response was not a JSON object",
            attempts=attempts, cloud=cloud,
        )
    if out.get("error"):
        raise ModelCallError(
            "request", _safe_model_error_detail(out.get("error")),
            attempts=attempts, cloud=cloud,
        )
    message = out.get("message")
    if isinstance(message, dict):
        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            # Free knowledge: the response itself proves this model reasons, so
            # the budget guard above needs no speculative /api/show probe.
            _remember_thinking_model(model)
            if reasoning_exposure_enabled():
                activity_tracker.record_reasoning(thinking, model=model)
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        if accept_native_tool_calls:
            native_decision = _native_tool_call_decision(message)
            if native_decision is not None:
                return out, native_decision
        if not cloud and not _budget_retried and _thinking_exhausted_budget(out, message):
            # The model reasoned right up to the cap and never got to an answer.
            # Now that the response has identified it, retry once with the
            # headroom it needed rather than reporting an empty response.
            return _chat_request(
                _with_local_thinking_budget(payload),
                model=model,
                cloud=cloud,
                timeout=timeout,
                cancel_check=cancel_check,
                accept_native_tool_calls=accept_native_tool_calls,
                idempotent=idempotent,
                _budget_retried=True,
            )
        raise ModelCallError(
            "empty_response",
            _empty_model_response_detail(out, message),
            attempts=attempts,
            cloud=cloud,
        )
    return out, content


def _model_usage_count(value):
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _empty_model_response_detail(out, message):
    """Describe an empty response without exposing model reasoning content."""
    metadata = {}
    if isinstance(message, dict):
        thinking = message.get("thinking")
        if isinstance(thinking, str):
            metadata["thinking_chars"] = len(thinking)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            metadata["tool_call_count"] = len(tool_calls)

    eval_count = _model_usage_count(out.get("eval_count"))
    if eval_count is not None:
        metadata["eval_count"] = eval_count

    done_reason = out.get("done_reason")
    if isinstance(done_reason, str) and done_reason.strip():
        normalized_reason = done_reason.strip().casefold()
        metadata["done_reason"] = (
            normalized_reason
            if normalized_reason in {"stop", "length"}
            else "other"
        )

    detail = "Ollama returned no assistant content"
    if metadata:
        detail += "; metadata=" + json.dumps(metadata, sort_keys=True)
    return detail


def _format_model_call_error(error: ModelCallError) -> str:
    target = (
        "hosted Ollama" if error.cloud else
        "remote Ollama" if not ollama_endpoint.is_loopback(BASE) else
        "local Ollama"
    )
    suffix = " after %d attempt(s)" % error.attempts
    if error.kind == "budget":
        return "ERROR: hosted agent output budget exhausted: %s" % error.detail
    if error.kind == "http":
        return "ERROR: %s rejected the model request (HTTP %s)%s: %s" % (
            target, error.status or "unknown", suffix, error.detail,
        )
    if error.kind == "configuration":
        return "ERROR: %s" % error.detail
    if error.kind in ("protocol", "empty_response", "request"):
        return "ERROR: invalid response from %s%s: %s" % (
            target, suffix, error.detail,
        )
    if error.kind == "cancelled":
        return "ERROR: %s" % error.detail
    return "ERROR contacting %s at %s%s: %s" % (
        target, _ollama_display(), suffix, error.detail,
    )


def _post(path: str, payload: dict, timeout: int | None = None) -> dict:
    _require_ollama_endpoint()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    request_timeout = _bounded_timeout(timeout)
    with ollama_endpoint.open_url(req, timeout=request_timeout) as resp:
        raw = resp.read(_MAX_MODEL_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_MODEL_RESPONSE_BYTES:
            raise ModelCallError(
                "protocol",
                "Ollama response exceeded the 16 MiB safety limit",
            )
        return json.loads(raw.decode("utf-8"))


_PREWARM_LOCK = threading.Lock()
_PREWARM_INFLIGHT = set()
# Bounds the background weight load so a wedged Ollama cannot hold the inflight
# slot forever. Previously written as a globals() lookup for a name that was
# never defined anywhere, so it always took the 60s fallback -- correct by
# accident, and reported by ruff as an undefined name.
_PREWARM_LOAD_TIMEOUT = 60


def prewarm_model(tier: str = "") -> bool:
    """Speculatively load the tier's local model while context is assembled.

    Model cold-load dominates first-token latency (tens of seconds for a 7B
    on CPU). Firing an empty keep-alive load concurrently with the host's
    DB/recall/augmentation work overlaps that cost, like a CPU prefetching a
    line it predicts the pipeline will need. Local tiers only, best-effort,
    one in-flight load per model, and never fatal: a failed prewarm just
    means the real call pays the normal cost.
    """
    if not sonder_speculation.speculation_enabled():
        return False
    try:
        model, cloud, _augment, tier_label = _serve_target(tier or "sonder", None)
    except Exception:
        return False
    if cloud or not model or tier_label in (None, "cloud-disabled"):
        return False
    with _PREWARM_LOCK:
        if model in _PREWARM_INFLIGHT:
            return False
        _PREWARM_INFLIGHT.add(model)

    def _load():
        try:
            # Empty prompt with keep_alive loads weights without generating.
            _post(
                "/api/generate",
                {"model": model, "keep_alive": KEEP_ALIVE},
                timeout=_PREWARM_LOAD_TIMEOUT,
            )
        except Exception:
            pass
        finally:
            with _PREWARM_LOCK:
                _PREWARM_INFLIGHT.discard(model)

    threading.Thread(
        target=_load, daemon=True, name="sonder-prewarm"
    ).start()
    return True


def _get(path: str) -> dict:
    _require_ollama_endpoint()
    req = urllib.request.Request(f"{BASE}{path}")
    with ollama_endpoint.open_url(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_schema_arg(schema):
    """Normalize an offload `schema` argument to a schema object, or None.

    Accepts an already-parsed object (internal callers) or the JSON text the
    tool surface passes (matching how every other structured argument crosses
    that boundary). A blank string means "no schema", so the unconstrained path
    stays the default. Anything else that is not a JSON object is a caller
    error and is raised as a typed configuration failure -- never quietly
    dropped, because dropping it would run the call unconstrained while the
    caller believed it was constrained.
    """
    if schema is None:
        return None
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, str):
        text = schema.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ModelCallError(
                "configuration",
                "schema is not valid JSON: %s" % exc,
            ) from exc
        if not isinstance(parsed, dict):
            raise ModelCallError(
                "configuration",
                "schema must be a JSON object, got %s" % type(parsed).__name__,
            )
        return parsed
    raise ModelCallError(
        "configuration",
        "schema must be a JSON object or JSON text, got %s" % type(schema).__name__,
    )


def _require_schema_match(text, schema):
    """Check a schema-constrained generation actually matches its schema.

    Ollama's ``format`` is applied by the backend we asked, which makes it a
    claim rather than evidence: a stale server, a model that ignores the
    grammar, or a truncated decode all produce text that never went through the
    constraint we think we imposed. Re-checking it here is cheap and makes the
    guarantee ours.

    A violation raises. It is deliberately not repaired, coerced, defaulted or
    silently re-asked -- rejecting non-conforming output is the entire point of
    requesting a schema, and a path that quietly fixes it up would return
    something that looks validated and is not. The failure names every path
    that did not validate so the caller can see what was wrong.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ModelCallError(
            "protocol",
            "response is not valid JSON despite a schema constraint: %s" % exc,
        ) from exc
    errors = json_schema_verifier.validate(data, schema)
    if errors:
        raise ModelCallError(
            "protocol", "schema violation: %s" % "; ".join(errors),
        )
    return data


def _file_schema_rejection(interaction_id):
    """File a schema violation as a caller-judged `rejected` outcome.

    A schema failure is the rare thing the outcome store is starved of: a
    negative verdict on delegated work, produced without anyone having to
    remember to file it. `grounded_outcomes` exists for exactly this -- taking
    the verdict from the tool that knows the truth instead of asking a human --
    and this writes through its own `record_fn` seam, `_record_outcome_signal`.
    It does not go through `grounded_outcomes.attribute`, because that resolves
    a *plausible* generation from a time-bounded ledger; here the failing
    interaction id is known exactly, and guessing would be strictly worse.

    `rejected` and not `failed`: `failed` is the machine-graded bucket that the
    self-generated curriculum floods by more than an order of magnitude, where
    a real caller-facing rejection is invisible to the only quality figure
    worth trusting. A conforming response deliberately files nothing -- matching
    a shape is not evidence that the answer was good, and recording `accepted`
    for it would raise the reviewed rate on something that never measured
    quality.
    """
    if not interaction_id:
        return
    try:
        _record_outcome_signal(interaction_id, "rejected")
    except Exception:
        # Bookkeeping must never mask the schema failure it is describing.
        pass


def _offload_impl(
    prompt: str,
    tier: str = "fast",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 0,
    learn: bool = True,
    timeout: int = TIMEOUT,
    cancel_check=None,
    schema=None,
) -> str:
    """Internal offload path; model failures stay typed for orchestrators."""
    schema = _parse_schema_arg(schema)
    # 0 means "ask the context policy", which the session path already does.
    # This path hardcoded 4096 and so ignored the policy and its env knobs,
    # which cost real capability: an autopilot run inspecting a 524 KB source
    # file looped on search because the file was 32x its window. Defer the
    # actual native size to context policy: CPU, Metal, AMD, Intel, NVIDIA, and
    # remote Ollama hosts have different KV-cache and memory ceilings.
    num_ctx = num_ctx or context_policy.native()
    _refresh_live_cloud_tiers()
    request_timeout = _bounded_timeout(timeout)
    model = TIERS.get(tier)
    if model is None:
        raise ModelCallError(
            "configuration",
            "unknown tier '%s'. Valid tiers: %s." % (tier, _valid_tier_names()),
        )
    cloud = _is_cloud_tier(tier, model)
    if cloud and not cloud_allowed():
        raise ModelCallError(
            "configuration",
            _cloud_disabled_message().removeprefix("ERROR: "),
            cloud=True,
        )

    if not _should_learn(tier, learn):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        if cloud:
            options = {"temperature": temperature, "num_predict": num_predict}
        else:
            options = _local_model_options(temperature, num_predict, num_ctx)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if schema is not None:
            payload["format"] = schema
        if cloud:
            _apply_cloud_thinking_policy(payload, model)
        else:
            payload["keep_alive"] = KEEP_ALIVE
        started = time.time()
        ok = False
        usage = {}
        used_model = model
        msg = ""
        try:
            if cloud:
                out, msg, used_model = _chat_request_with_cloud_fallback(
                    payload,
                    model=model,
                    timeout=request_timeout,
                    cancel_check=cancel_check,
                )
            else:
                out, msg = _chat_request(
                    payload,
                    model=model,
                    cloud=False,
                    timeout=request_timeout,
                    cancel_check=cancel_check,
                    idempotent=True,
                )
            tokens_in = _model_usage_count(out.get("prompt_eval_count"))
            tokens_out = _model_usage_count(out.get("eval_count"))
            source = (
                "ollama"
                if tokens_in is not None or tokens_out is not None
                else "estimated"
            )
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
            if schema is not None:
                # A response that did not honour the schema is a failed call,
                # not a successful one with bad content -- so this runs before
                # `ok`, and the tracker records the attempt as a failure.
                _require_schema_match(msg, schema)
            ok = True
            return msg
        finally:
            activity_tracker.record_model_call(
                model=used_model,
                prompt_chars=len(prompt or ""),
                history_messages=0,
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                token_source=usage.get("token_source", ""),
                request_preview=prompt,
                response_preview=msg if ok else None,
                ok=ok,
                elapsed_ms=int((time.time() - started) * 1000),
            )

    retrieve_kwargs = {}
    if cloud:
        gen = _make_generate(
            model,
            system,
            temperature,
            num_predict,
            num_ctx,
            cloud=True,
            timeout=request_timeout,
            cancel_check=cancel_check,
            schema=schema,
        )
        retrieve_kwargs["retrieve_fn"] = _no_retrieve
    else:
        learning_model = resolve_sonder_model(_STRICT_DEFAULT)
        if learning_model is None:
            raise ModelCallError(
                "configuration",
                "`sonder:latest` Ollama alias not found. Run setup_alias.py, "
                "or call with strict=False to fall back to the base coder.",
            )
        gen = _make_generate(
            learning_model,
            system,
            temperature,
            num_predict,
            num_ctx,
            timeout=request_timeout,
            cancel_check=cancel_check,
            schema=schema,
        )
    conn = _open_db()
    try:
        response, iid = orchestrator.run_with_learning(
            conn, prompt, tier, gen, **retrieve_kwargs,
        )
    finally:
        conn.close()
    if schema is not None:
        try:
            _require_schema_match(response, schema)
        except ModelCallError:
            _file_schema_rejection(iid)
            raise
    return with_footer(response, iid)


@mcp.tool()
def offload(
    prompt: str,
    tier: str = "fast",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 0,
    learn: bool = True,
    timeout: int = TIMEOUT,
    schema: str = "",
) -> str:
    """Offload a self-contained subtask to a local or Ollama-cloud model.

    Local aliases (fast/code/general, plus reasoning/vision when the operator
    binds them) run privately through loopback Ollama by default on CPU or any
    accelerator Ollama supports; an explicitly opted-in remote OLLAMA_HOST
    leaves this machine. The learning tiers
    (SONDER_LEARN_TIERS, default: every configured local tier) participate in the
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

    schema: optional JSON Schema *as JSON text*, e.g.
    '{"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}'.
    Supplying it constrains the model's decoder to that shape AND re-checks the
    returned text against it here. A response that does not match is REJECTED
    with the failing path named -- it is never repaired into a passing one, and
    there is no silent retry. Omit it (the default) and this call behaves
    exactly as it always has: no format constraint, no validation, raw text back.
    Small object shapes work far better than deep ones on a 3B/7B local tier.
    """
    _maybe_live_reload()
    try:
        return _offload_impl(
            prompt=prompt,
            tier=tier,
            system=system,
            temperature=temperature,
            num_predict=num_predict,
            num_ctx=num_ctx,
            learn=learn,
            timeout=timeout,
            schema=schema,
        )
    except ModelCallError as error:
        return _format_model_call_error(error)


def _env_location_consent() -> bool:
    """Opt-in approximate-IP-location consent for the local MCP/REPL surfaces.

    Off by default to preserve the privacy contract. Set
    SONDER_LOCATION_CONSENT=1 to allow server-side approximate location lookup
    on this host's own chat surfaces.
    """
    return os.environ.get("SONDER_LOCATION_CONSENT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _session_messages_light(
    conn, session_id, max_turns=None, project=_ALL_PROJECTS,
):
    """Recent session turns as chat messages, without summarization side effects."""
    msgs = []
    limit = MAX_TURNS if max_turns is None else max_turns
    if project is _ALL_PROJECTS:
        turns = memory_store.session_turns(conn, session_id)
    else:
        turns = memory_store.session_turns_for_project(conn, session_id, project)
    if limit and limit > 0:
        turns = turns[-limit:]
    for turn in turns:
        msgs.append({"role": "user", "content": turn["task"]})
        msgs.append({"role": "assistant", "content": turn["response"]})
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
            history = _session_messages_light(
                conn, session_id, project=_resolve_project(project),
            )
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
                session_id=session_id, project=_resolve_project(project),
                project_explicit=True,
            )
        except Exception:
            pass
        finally:
            conn.close()
    return reply


def _sonder_impl_serialized(
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
    internal_generate = _internal_generate_for_route(tgt_model, cloud)
    effective_system = _build_system(system, trace, persona)

    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    requested_ctx = _context_requested(context_size or (SESSION_NUM_CTX if session_id else num_ctx))
    # Sessioned threads get the selected virtual context window; honor a larger explicit num_ctx.
    num_ctx_eff = max(num_ctx, requested_ctx) if session_id else requested_ctx

    interaction_snapshot = None
    conn = _open_db()
    try:
        history = None
        is_first = False
        if session_id:
            is_first = memory_store.session_turn_count(conn, session_id) == 0
            memory_store.touch_session(conn, session_id, project_id)
            history = _session_history_messages(
                conn, session_id, MAX_TURNS, project=project_id,
                internal_generate=internal_generate,
            )
        response, iid, trace_ctx = _answer(
            conn, prompt, tgt_model, effective_system, temperature, num_predict,
            num_ctx_eff, session_id, project_id, history, trace=trace,
            tier=tier_label, cloud=cloud, augment=augment,
        )
        _capture_turn(tgt_model, tier_label, trace_ctx, prompt, response, iid)
        if iid is not None:
            interaction_snapshot = memory_store.get_interaction(conn, iid)
        if session_id and is_first:
            _maybe_title(
                conn, session_id, prompt,
                internal_generate=internal_generate,
            )
    except ModelCallError as error:
        return _format_model_call_error(error)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (_ollama_display(), e))
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

    captured_response = response
    repair_usage = {}

    def _code_repair(repair_prompt):
        gen = _make_generate(
            tgt_model, effective_system, temperature, num_predict,
            num_ctx_eff, cloud=cloud,
        )
        repair_history = list(history or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": captured_response},
        ]
        repaired_response = gen(repair_prompt, repair_history)
        repair_usage.clear()
        repair_usage.update(getattr(gen, "last_usage", None) or {})
        return repaired_response

    response, _code_verified, code_repaired = _apply_code_gate(
        response, interaction_id=iid, regenerate=_code_repair,
    )
    footer_iid = iid
    if code_repaired and not _persist_verified_code_repair(
        iid, interaction_snapshot, response, repair_usage,
    ):
        footer_iid = None

    if trace:
        params = {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": num_ctx_eff,
            "num_ctx_native": _context_native(num_ctx_eff),
        }
        trace_block = _format_trace(tgt_model, tier_label, params, trace_ctx)
        # Footer must stay LAST so parse_interaction_id's $-anchored regex still finds it.
        traced_response = response + trace_block
        return (
            with_footer(traced_response, footer_iid)
            if footer_iid is not None else _append_activity(traced_response)
        )
    return (
        with_footer(response, footer_iid)
        if footer_iid is not None else _append_activity(response)
    )


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
    session_id = _resolve_session(session)
    with _serialized_session_turn(session_id):
        claim = None
        if session_id is not None:
            claim, claim_error = _acquire_persistent_session_turn(session_id)
            if claim is None:
                return claim_error
        try:
            return _sonder_impl_serialized(
                prompt=prompt,
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
        finally:
            _release_persistent_session_turn(claim)


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
    raise_model_errors=False,
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
    interaction_snapshot = None
    conn = _open_db()
    try:
        if session_id:
            memory_store.touch_session(conn, session_id, project_id)
        if learn:
            # Augmentation policy controls what the model sees, not provenance.
            # Even clean/cloud teacher turns retain their explicit project so a
            # later grounded outcome cannot become an unscoped raw recall.
            capture_project = project_id
            response, iid, trace_ctx = _answer(
                conn, prompt, model, effective_system, 0.2, 1024, req_ctx,
                session_id, capture_project, history or None, trace=trace,
                tier=tier_label, cloud=cloud, augment=augment,
            )
            _capture_turn(model, tier_label, trace_ctx, prompt, response, iid)
            if iid is not None:
                interaction_snapshot = memory_store.get_interaction(conn, iid)
        else:
            gen = _make_generate(model, effective_system, 0.2, 1024,
                                 req_ctx, cloud=cloud)
            response = gen(prompt, history or None)
            iid, trace_ctx = None, None
    except ModelCallError as error:
        if raise_model_errors:
            raise
        return _format_model_call_error(error)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (_ollama_display(), e))
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

    captured_response = response
    repair_usage = {}

    def _code_repair(repair_prompt):
        gen = _make_generate(model, effective_system, 0.2, 1024, req_ctx,
                             cloud=cloud)
        repair_history = list(history or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": captured_response},
        ]
        repaired_response = gen(repair_prompt, repair_history)
        repair_usage.clear()
        repair_usage.update(getattr(gen, "last_usage", None) or {})
        return repaired_response

    response, _code_verified, code_repaired = _apply_code_gate(
        response, interaction_id=iid, regenerate=_code_repair,
    )
    footer_iid = iid
    if code_repaired and not _persist_verified_code_repair(
        iid, interaction_snapshot, response, repair_usage,
    ):
        footer_iid = None
    if trace and trace_ctx is not None:
        params = {
            "temperature": 0.2,
            "num_predict": 1024,
            "num_ctx": req_ctx,
            "num_ctx_native": _context_native(req_ctx),
        }
        trace_block = _format_trace(model, tier_label, params, trace_ctx)
        traced_response = response + trace_block
        return (
            with_footer(traced_response, footer_iid)
            if footer_iid is not None else _append_activity(traced_response)
        )
    if footer_iid is not None:
        return with_footer(response, footer_iid)
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
    raise_model_errors=False,
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
            raise_model_errors=raise_model_errors,
        )
    return _append_activity(result, response=response, replace=True)


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into sonder's learning loop.

    Call this after a sonder/offload response once you know how it went.
    Pass the id from the '[interaction_id: <id>]' footer of the response.
    A good outcome triggers a distilled 'lesson' that future prompts retrieve.

    Signals split into two populations that health reporting keeps apart,
    because averaging them produces a number that reads like accuracy and is
    not one:

      JUDGED BY YOU -- used, copied, edited, accepted, rejected.
        Prefer these. This is the population `reviewed_positive_percent`
        measures: how often work a caller delegated turned out to be good.

      MACHINE-GRADED -- tests_passed, failed, compiled.
        Reserved for a runner reporting what the code did. The self-generated
        curriculum floods these (7000+ rows against ~190 judged), so anything
        recorded here is effectively invisible in the reviewed rate.

    So if YOU ran the tests and are reporting your own verdict, prefer
    `accepted` or `rejected` over `tests_passed`/`failed` -- otherwise a real
    caller judgement lands in the self-marked bucket and stops counting toward
    the only quality figure anyone should trust. The split is inferred from the
    signal name; there is no recorded source, so this is a convention the caller
    has to honour rather than something the runtime can enforce.
    """
    _maybe_live_reload()
    if signal not in reward.VALID_SIGNALS:
        return "ERROR: unknown signal '%s'. Valid: %s." % (
            signal, ", ".join(sorted(reward.VALID_SIGNALS)))
    result = _record_outcome_and_maybe_distill(interaction_id, signal)
    if not result["found"]:
        return "ERROR: no interaction '%s' (already expired or wrong id)." % interaction_id
    verb = "Recorded" if result["outcome_inserted"] else "Already recorded"
    msg = "%s '%s' (reward %+.2f) for %s." % (
        verb, signal, result["reward"], interaction_id,
    )
    if result["lesson_id"]:
        msg += " Distilled lesson %s." % result["lesson_id"]
    elif result["distillation_deferred"]:
        msg += " Lesson distillation was deferred for retry."
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
    _refresh_live_cloud_tiers()
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
    _refresh_live_cloud_tiers()
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
    # Harder algorithmic tier: these force the failure classes small local
    # models actually exhibit (incomplete map initialization, eviction-order
    # bookkeeping, interval bookkeeping, stack discipline) so campaign
    # records and distilled lessons carry real signal, not just boilerplate.
    ("toposort",
     "topologically sort the directed edges d->a, a->b, b->c, a->c (an edge "
     "u->v means u comes before v; include every node) and print the only "
     "valid order as space-separated letters on one line, like: w x y z"),
    ("lru",
     "simulate an LRU cache with capacity 2 and operations put(1,10), "
     "put(2,20), get(1), put(3,30) (this evicts the least recently used "
     "key), get(2), get(3); print the three get results on one line "
     "separated by single spaces, using -1 for a miss"),
    ("intervals",
     "merge the overlapping intervals [1,3] [2,6] [8,10] [9,12] and print "
     "the merged intervals on one line as start-end pairs separated by a "
     "single space, like 5-7 9-11"),
    ("balanced",
     # Two harness lessons are baked into this wording. (1) The pass check is
     # a containment check, so wrong verdicts must not contain the expected
     # text as a substring: 'ok'/'bad' cannot embed each other the way
     # 'valid'/'invalid' can. (2) Bracket literals must be quoted and
     # numbered, never wrapped in bare parentheses — models read those outer
     # parens as punctuation and silently drop them, then get marked wrong
     # for answering a different question than the one intended.
     "decide whether the brackets are balanced in each of these three "
     "strings, given exactly between the quotes: "
     "1) \"([]{})\"  2) \"([)]\"  3) \"(((\" . "
     "Print ok if that string's brackets are balanced or bad if they are "
     "not, one verdict per line, in that order"),
    ("wordfreq",
     "count word frequencies in the sentence 'the quick the lazy the end' "
     "and print the most frequent word and its count as word:count"),
    ("fib",
     "print the 20th Fibonacci number where fib(1)=1 and fib(2)=1"),
]


def _campaign_expected(task_name):
    return {
        "hello": "sonder-ok",
        "sum": "42",
        "loop": "1\n2\n3",
        "string": "rednos",
        "branch": "prime",
        "list": "20",
        "toposort": "d a b c",
        "lru": "10 -1 30",
        "intervals": "1-6 8-12",
        "balanced": "ok\nbad\nbad",
        "wordfreq": "the:3",
        "fib": "6765",
    }.get(task_name, "")


def _campaign_output_matches(output, expected):
    """Whether executed output satisfies a task that says "print exactly".

    Substring containment let a chatty answer false-pass: "The result of 12 +
    30 is 42." contains "42" and was recorded as a success even though every
    campaign task asks to print the value exactly. The comparison is now the
    faithful one - each line stripped, blank leading/trailing lines dropped,
    platform line-endings normalised, then equal - which tolerates trailing
    whitespace and newlines while rejecting embedded prose and extra lines.
    """
    def _norm(text):
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        stripped = [line.strip() for line in lines]
        while stripped and not stripped[0]:
            stripped.pop(0)
        while stripped and not stripped[-1]:
            stripped.pop()
        return "\n".join(stripped)

    return _norm(output) == _norm(expected)


def _campaign_environment_failure(output):
    """Whether a failed attempt broke on the host, not the model.

    A missing interpreter/compiler fails every attempt in that language no
    matter what the model wrote; recording it as 'failed' would penalize the
    model for the host's toolchain - the same mis-attribution class as the
    pytest timeouts campaign_repo_repair no longer records. A repair round
    cannot install a toolchain either, so callers also stop retrying.
    """
    return str(output or "").startswith("missing runtime/compiler:")


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
        # Advance the task only after a full pass over the languages. Indexing
        # both by i pairs them on a fixed residue whenever the two counts share
        # a factor: 3 languages against 12 tasks gave each language just 4 of
        # them, and PowerShell could never draw the three tasks it actually
        # fails - so a 30/30 sweep proved nothing about the hard combinations.
        task_name, task_text = _CAMPAIGN_TASKS[
            (i // len(language_list)) % len(_CAMPAIGN_TASKS)
        ]
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
                if ok and expected and not _campaign_output_matches(out, expected):
                    ok = False
                    out = "wrong output; expected exactly %r, got %r" % (expected, out)
            record_msg = ""
            pitfall_note = ""
            env_failure = (not ok) and _campaign_environment_failure(out)
            if ok and iid:
                with _CAMPAIGN_LEARN_LOCK:
                    record_msg = record_outcome(iid, "tests_passed")
            elif env_failure:
                # Host toolchain breakage: the model was never judged, so no
                # outcome and no pitfall are recorded against it.
                pass
            elif attempt == repair_rounds and record_failures and iid:
                with _CAMPAIGN_LEARN_LOCK:
                    record_msg = record_outcome(iid, "failed")
                    # A failure that teaches nothing recurs forever; distill
                    # the guard while the error is still in hand.
                    pitfall, pitfall_note = _record_failure_pitfall(
                        iid, prompt, code, out,
                    )
                if pitfall:
                    record_msg += " Distilled pitfall %s." % pitfall
                elif pitfall_note:
                    record_msg += " " + pitfall_note
            attempts.append({
                "attempt": attempt + 1,
                "ok": ok,
                "iid": iid,
                "output": out,
                "record": record_msg,
                "pitfall_error": pitfall_note,
                "env_skipped": env_failure,
            })
            if ok or env_failure:
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
    # A failure whose pitfall distillation raised teaches nothing, and the
    # unattended runner only ever logs these summary lines - so a note buried
    # in a per-attempt record is invisible exactly when it matters most.
    # Reported only when non-zero, to keep a healthy run quiet.
    pitfall_errors = [
        a.get("pitfall_error")
        for r in results
        for a in r.get("attempts", [])
        if a.get("pitfall_error")
    ]
    env_skipped = sum(
        1
        for r in results
        for a in r.get("attempts", [])
        if a.get("env_skipped")
    )
    by_lang = {}
    for r in results:
        lang = r["language"]
        ok, total_lang = by_lang.get(lang, (0, 0))
        by_lang[lang] = (ok + (1 if r["ok"] else 0), total_lang + 1)
    lines = [
        _campaign_headline(
            passed, len(results), recorded, failed_recorded,
            len(pitfall_errors), elapsed,
        ),
        "by language: %s" % ", ".join(
            "%s=%d/%d" % (lang, ok, total_lang)
            for lang, (ok, total_lang) in sorted(by_lang.items())
        ),
    ]
    if pitfall_errors:
        lines.append("first pitfall error: %s" % pitfall_errors[0])
    if env_skipped:
        lines.append(
            "environment failures skipped, not recorded against the model: %d"
            % env_skipped,
        )
    drain = _drain_deferred_distillations(limit=max(16, len(results)))
    if drain["drained"]:
        lines.append(
            "deferred distillations drained: %d (lessons stored %d, "
            "still deferred in batch %d, backlog remaining %s)"
            % (
                drain["drained"], drain["stored"], drain["deferred"],
                _drain_backlog_text(drain),
            ),
        )
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


# Planted-bug repair templates: (name, buggy module source, pytest source).
# Each bug is a class small local models actually exhibit; the fix is judged
# by the project's own tests, not output matching, so lessons distilled from
# these interactions reflect real repair work.
_REPO_REPAIR_TASKS = [
    ("offbyone",
     "def total(values):\n"
     "    result = 0\n"
     "    for index in range(len(values) - 1):\n"
     "        result += values[index]\n"
     "    return result\n",
     "from module import total\n\n\n"
     "def test_total_includes_every_value():\n"
     "    assert total([1, 2, 3, 4]) == 10\n\n\n"
     "def test_total_single_value():\n"
     "    assert total([7]) == 7\n\n\n"
     "def test_total_empty():\n"
     "    assert total([]) == 0\n"),
    ("boundary",
     "def bulk_discount(quantity):\n"
     "    # spec: orders of 10 or more get the discount\n"
     "    return 0.1 if quantity > 10 else 0.0\n",
     "from module import bulk_discount\n\n\n"
     "def test_discount_at_threshold():\n"
     "    assert bulk_discount(10) == 0.1\n\n\n"
     "def test_discount_above_threshold():\n"
     "    assert bulk_discount(11) == 0.1\n\n\n"
     "def test_no_discount_below():\n"
     "    assert bulk_discount(9) == 0.0\n"),
    ("mutabledefault",
     "def add_tag(tag, tags=[]):\n"
     "    tags.append(tag)\n"
     "    return tags\n",
     "from module import add_tag\n\n\n"
     "def test_fresh_list_per_call():\n"
     "    assert add_tag('a') == ['a']\n"
     "    assert add_tag('b') == ['b']\n\n\n"
     "def test_explicit_list_still_appends():\n"
     "    existing = ['x']\n"
     "    assert add_tag('y', existing) == ['x', 'y']\n"),
    ("missingkey",
     "def word_counts(words):\n"
     "    counts = {}\n"
     "    for word in words:\n"
     "        counts[word] += 1\n"
     "    return counts\n",
     "from module import word_counts\n\n\n"
     "def test_counts_new_words():\n"
     "    assert word_counts(['a', 'b', 'a']) == {'a': 2, 'b': 1}\n\n\n"
     "def test_counts_empty():\n"
     "    assert word_counts([]) == {}\n"),
    ("numericsort",
     "def sort_ids(ids):\n"
     "    # ids arrive as decimal strings; callers need numeric order\n"
     "    return sorted(ids)\n",
     "from module import sort_ids\n\n\n"
     "def test_numeric_not_lexicographic():\n"
     "    assert sort_ids(['10', '2', '1']) == ['1', '2', '10']\n\n\n"
     "def test_already_sorted():\n"
     "    assert sort_ids(['1', '2']) == ['1', '2']\n"),
    # A second tier of bug classes. The first five became memorised - 10/10 on
    # every nightly run, which measures recall rather than repair - so these
    # cover failure modes the originals miss: aliasing a caller's object,
    # integer division, short-circuit ordering against None, accumulating in
    # the wrong scope, and an exclusive range boundary.
    ("aliasing",
     "def with_defaults(config):\n"
     "    # must not disturb the caller's dict\n"
     "    merged = config\n"
     "    merged.setdefault('retries', 3)\n"
     "    return merged\n",
     "from module import with_defaults\n\n\n"
     "def test_caller_dict_is_untouched():\n"
     "    original = {'host': 'localhost'}\n"
     "    result = with_defaults(original)\n"
     "    assert result == {'host': 'localhost', 'retries': 3}\n"
     "    assert original == {'host': 'localhost'}\n\n\n"
     "def test_explicit_value_wins():\n"
     "    assert with_defaults({'retries': 9})['retries'] == 9\n"),
    ("intdivision",
     "def average(values):\n"
     "    return sum(values) // len(values)\n",
     "from module import average\n\n\n"
     "def test_average_is_not_truncated():\n"
     "    assert average([1, 2]) == 1.5\n\n\n"
     "def test_exact_average_stays_exact():\n"
     "    assert average([2, 4]) == 3\n"),
    ("noneguard",
     "def name_length(user):\n"
     "    # user may be None, or may have no name set\n"
     "    return len(user['name']) if user['name'] else 0\n",
     "from module import name_length\n\n\n"
     "def test_missing_user_is_zero():\n"
     "    assert name_length(None) == 0\n\n\n"
     "def test_empty_name_is_zero():\n"
     "    assert name_length({'name': ''}) == 0\n\n\n"
     "def test_real_name_is_counted():\n"
     "    assert name_length({'name': 'ada'}) == 3\n"),
    ("wrongscope",
     "def group_lengths(words):\n"
     "    # one bucket per length\n"
     "    buckets = {}\n"
     "    found = []\n"
     "    for word in words:\n"
     "        found.append(word)\n"
     "        buckets[len(word)] = found\n"
     "    return buckets\n",
     "from module import group_lengths\n\n\n"
     "def test_each_bucket_holds_only_its_own_words():\n"
     "    assert group_lengths(['a', 'bb', 'cc']) == {1: ['a'], 2: ['bb', 'cc']}\n\n\n"
     "def test_empty_input():\n"
     "    assert group_lengths([]) == {}\n"),
    ("rangeend",
     "def inclusive_range(start, end):\n"
     "    # callers expect both endpoints included\n"
     "    return list(range(start, end))\n",
     "from module import inclusive_range\n\n\n"
     "def test_end_is_included():\n"
     "    assert inclusive_range(1, 4) == [1, 2, 3, 4]\n\n\n"
     "def test_single_value_range():\n"
     "    assert inclusive_range(2, 2) == [2]\n"),
]


def _repo_repair_pytest(workdir, timeout):
    """Run pytest for one scratch project; (ok, bounded output, infra_error).

    ``infra_error`` is non-empty only when the verdict says nothing about the
    candidate code: a timeout, a failed spawn, or pytest itself breaking.
    Recording those as model failures poisons the reward store with negative
    signals the model did not earn (observed 2026-08-02: a memory-starved
    20-job run banked 20 bogus ``failed`` outcomes). The converse matters just
    as much - a candidate that fails to even import is the model's fault and
    must stay attributable.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-x"],
            cwd=str(workdir), capture_output=True, text=True,
            # This server's own stdin is the MCP protocol pipe. A child that
            # inherits it can block forever on a read nobody will answer —
            # exactly how every job in a 20-job run timed out while the
            # identical call from a child process took 0.8s.
            stdin=subprocess.DEVNULL,
            timeout=max(5, timeout),
            env=sonder_logging.child_environment(),
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out", "pytest timed out"
    except OSError as exc:
        return False, str(exc)[:200], "pytest could not start: %s" % (
            type(exc).__name__,
        )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # pytest exit codes: 0 passed, 1 test failed, 2 interrupted (a collection
    # error), 3 internal error, 4 usage error, 5 no tests collected.
    #
    # 2 is the candidate's own fault and must stay attributable. A module the
    # model wrote with a SyntaxError fails at import, pytest aborts collection,
    # and the error is reported against the test file that imported it -
    # observed live when a generation leaked activity-log text into module.py.
    # Treating 2 as infrastructure excused a real model failure, which is the
    # mirror image of the bug this split was added to fix.
    #
    # 3/4/5 say nothing about the candidate: pytest broke, was misinvoked, or
    # the test file never arrived. Those stay unattributable, as do a timeout
    # and a failed spawn above.
    infra = ""
    if proc.returncode not in (0, 1, 2):
        infra = "pytest exited %d without a test verdict" % proc.returncode
    return proc.returncode == 0, output[-1500:], infra


@mcp.tool()
def campaign_repo_repair(
    total: int = 10,
    tier: str = "code",
    max_workers: int = 4,
    timeout: int = 30,
    repair_rounds: int = 1,
    record_failures: bool = True,
) -> str:
    """Repair planted bugs in scratch projects, verified by their own tests.

    Each job materializes a small Python project whose test suite fails on a
    deliberately planted bug, shows the model the module and the failing
    pytest output, and accepts only a corrected module that makes the whole
    suite pass. Passing repairs record tests_passed; terminal failures record
    failed, so the reward store learns from genuine repair work.
    """
    import shutil
    import tempfile

    total = max(1, min(int(total or 1), 60))
    max_workers = max(1, min(int(max_workers or 1), 8, total))
    timeout = max(5, min(int(timeout or 30), 120))
    repair_rounds = max(0, min(int(repair_rounds or 0), 3))
    jobs = [
        (i, *_REPO_REPAIR_TASKS[i % len(_REPO_REPAIR_TASKS)])
        for i in range(total)
    ]

    def run_one(index, task_name, module_src, test_src):
        workdir = Path(tempfile.mkdtemp(prefix="sonder-repair-"))
        attempts = []
        try:
            (workdir / "module.py").write_text(module_src, encoding="utf-8")
            (workdir / "test_module.py").write_text(
                test_src, encoding="utf-8")
            ok, failure, infra = _repo_repair_pytest(workdir, timeout)
            if infra or ok:
                return {
                    "index": index, "task": task_name, "ok": False,
                    "attempts": [], "iid": None,
                    "error": infra or "template did not fail before repair",
                    "infra": bool(infra),
                }
            current_module = module_src
            for attempt in range(repair_rounds + 1):
                prompt = (
                    "A small Python project fails its tests because of a bug "
                    "in module.py. Return the complete corrected module.py "
                    "in one ```python code block. Do not modify or restate "
                    "the tests.\n\nmodule.py:\n```python\n%s```\n\n"
                    "Failing pytest output:\n%s"
                    % (current_module, failure[-900:])
                )
                with _CAMPAIGN_LEARN_LOCK:
                    response = sonder(
                        prompt, tier=tier, session="none",
                        temperature=0.2, num_predict=700,
                    )
                iid = parse_interaction_id(response)
                code = grounding.extract_code_block(response, "python")
                infra = ""
                if code:
                    (workdir / "module.py").write_text(
                        code, encoding="utf-8")
                    current_module = code
                    ok, failure, infra = _repo_repair_pytest(
                        workdir, timeout)
                else:
                    ok, failure = False, "no python code block returned"
                record_msg = ""
                pitfall_note = ""
                if infra:
                    # No attributable verdict: leave the reward store alone.
                    attempts.append({
                        "attempt": attempt + 1, "ok": False, "iid": iid,
                        "output": failure[-400:], "record": "",
                        "infra": infra, "pitfall_error": "",
                    })
                    break
                if ok and iid:
                    with _CAMPAIGN_LEARN_LOCK:
                        record_msg = record_outcome(iid, "tests_passed")
                elif (
                    attempt == repair_rounds and record_failures and iid
                ):
                    with _CAMPAIGN_LEARN_LOCK:
                        record_msg = record_outcome(iid, "failed")
                        # infra failures already returned above, so this is
                        # an attributable model failure worth learning from.
                        pitfall, pitfall_note = _record_failure_pitfall(
                            iid, prompt, code or "", failure,
                        )
                    if pitfall:
                        record_msg += " Distilled pitfall %s." % pitfall
                    elif pitfall_note:
                        record_msg += " " + pitfall_note
                attempts.append({
                    "attempt": attempt + 1, "ok": ok, "iid": iid,
                    "output": failure[-400:], "record": record_msg,
                    "infra": "", "pitfall_error": pitfall_note,
                })
                if ok:
                    break
            final = attempts[-1]
            return {
                "index": index, "task": task_name,
                "ok": bool(final["ok"]), "attempts": attempts,
                "iid": final.get("iid"),
                "error": final.get("infra") or "",
                "infra": bool(final.get("infra")),
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    started = time.time()
    results = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_one, *job) for job in jobs]
        for future in as_completed(futures):
            outcome = future.result()
            results[outcome["index"]] = outcome
    elapsed = round(time.time() - started, 3)
    passed = sum(1 for r in results if r and r.get("ok"))
    infra_skipped = sum(1 for r in results if r and r.get("infra"))
    # Same blindness as the campaign tier: only this first line survives into
    # an unattended run's log, so a crashed pitfall distillation has to be
    # counted here or it is never seen.
    pitfall_errors = sum(
        1
        for r in results if r
        for a in r.get("attempts", [])
        if a.get("pitfall_error")
    )
    lines = [
        "campaign repo-repair: %d/%d suites fixed in %.3fs%s%s"
        % (passed, len(results), elapsed,
           (" (%d job(s) unattributable — no outcome recorded)"
            % infra_skipped) if infra_skipped else "",
           (" (%d pitfall-error(s))" % pitfall_errors)
           if pitfall_errors else ""),
    ]
    if infra_skipped and infra_skipped >= max(1, len(results) // 2):
        lines.append(
            "WARNING: most jobs failed for infrastructure reasons (commonly "
            "memory pressure with too many workers) — lower max_workers or "
            "free memory; these jobs taught the model nothing.",
        )
    drain = _drain_deferred_distillations(limit=max(16, len(results)))
    if drain["drained"]:
        lines.append(
            "deferred distillations drained: %d (lessons stored %d, "
            "still deferred in batch %d, backlog remaining %s)"
            % (
                drain["drained"], drain["stored"], drain["deferred"],
                _drain_backlog_text(drain),
            ),
        )
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        lines.append("[%s] %s-%d attempts=%d iid=%s%s" % (
            status, r["task"], r["index"] + 1, len(r.get("attempts") or []),
            r.get("iid") or "-",
            (" (%s)" % r["error"]) if r.get("error") else "",
        ))
        attempts = r.get("attempts") or []
        if attempts and not r["ok"]:
            lines.append((attempts[-1].get("output") or "")[:400])
        if attempts and attempts[-1].get("record"):
            lines.append(attempts[-1]["record"][:200])
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
        scoped_turns = (
            memory_store.session_turns_for_project(conn, session_id, project_id)
            if session_id else []
        )
        turns = [
            (row["task"], row["response"])
            for row in scoped_turns[-MAX_TURNS:]
        ]
        session_row = memory_store.get_session(conn, session_id) if session_id else None
        summary_row = (
            memory_store.get_session_project_summary(conn, session_id, project_id)
            if session_id else {}
        )
        summary = summary_row.get("summary") or ""
        turn_count = len(scoped_turns)
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        lesson_count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        fact_count = (
            memory_store.count_facts(conn, project_id) if project_id else
            conn.execute("SELECT COUNT(*) FROM facts WHERE project IS NULL").fetchone()[0]
        )
        preference_count = conn.execute(
            "SELECT COUNT(*) FROM preferences WHERE enabled=1"
        ).fetchone()[0]
        interaction_count = memory_store.count_interactions(conn)
        outcome_count = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        summarized_through = summary_row.get("summarized_through") or ""
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
    source = activity_tracker.snapshot()
    snap = activity_tracker.public_snapshot(source)
    if snap is None:
        return "sonder activity\n  state: unknown"
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
    if include_events:
        lines.extend([
            "",
            activity_tracker.format_execution_feed(
                activity_tracker.execution_feed(source)
            ),
        ])
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
    # Validate before applying. Previously an invalid value (non-numeric,
    # negative) silently fell back to the 8192 default and reported success,
    # overwriting the prior valid size; and 0 produced a 1-token context. Reject
    # junk and degenerate sizes with a clear error instead of quietly changing
    # state. An empty value keeps the "reset to default" convenience.
    if context_size not in (None, ""):
        parsed = context_policy.parse_strict(context_size)
        if parsed is None:
            return ("ERROR: invalid context size %r. Use a positive integer, "
                    "optionally suffixed k/m (e.g. 8192, 32k, 1m)." % context_size)
        if parsed < context_policy.MIN_CONTEXT:
            return ("ERROR: context size %d is below the %d-token minimum; a "
                    "context that small makes inference unusable."
                    % (parsed, context_policy.MIN_CONTEXT))
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


def _task_service(conn):
    return task_use_cases.TaskService(
        task_state_adapter.LegacyTaskRepository(conn),
        task_state_adapter.LegacyChecklistEventSink(activity_tracker.set_checklist),
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
        row = _task_service(conn).create_task(
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
    return "task created\n  " + _format_task(row.to_dict())


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
        rows = _task_service(conn).list_tasks(
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
        lines.append("  " + _format_task(row.to_dict()))
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
        row = _task_service(conn).update_task(
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
    return "task updated\n  " + _format_task(row.to_dict())


@mcp.tool()
def task_show(task_id: str, events: bool = True) -> str:
    """Show one task and its recent visible event history."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        detail = _task_service(conn).show_task(task_id, include_events=events)
    finally:
        conn.close()
    if not detail.task:
        return "ERROR: no task '%s'." % task_id
    lines = ["task", "  " + _format_task(detail.task.to_dict())]
    if detail.events:
        lines.append("events:")
        for event in detail.events:
            lines.append("  %(ts)s  %(event)s  %(note)s" % event)
    return "\n".join(lines)


@mcp.tool()
def task_delete(task_id: str) -> str:
    """Delete a task and all its children, events, and dependencies."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        result = _task_service(conn).delete_task(task_id)
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "deleted task %s (removed %d children)" % (
        result["deleted"][:8], result["children_removed"]
    )


@mcp.tool()
def task_plan(
    title: str,
    steps: str,
    project: str = "",
    owner: str = "agent",
    priority: int = 2,
    sequential: bool = True,
) -> str:
    """Batch-create a work plan: a parent task with ordered steps.

    steps is a JSON array of strings or {title, detail} objects.
    When sequential=True (default), each step depends on the previous one.
    """
    _maybe_live_reload()
    import json as _json
    try:
        parsed = _json.loads(steps) if isinstance(steps, str) else steps
    except (ValueError, TypeError) as e:
        return "ERROR: steps must be valid JSON array: %s" % e
    conn = _open_db()
    try:
        service = _task_service(conn)
        checklist = service.plan_tasks(
            title=title, steps=parsed,
            project=project, owner=owner, priority=priority,
            sequential=bool(sequential),
        )
        data = checklist.to_dict()
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    # Publish so a bare /checklist and the app's activity pane resolve to the
    # plan that was just created, the same way checklist_create does.
    service.publish_checklist(checklist)
    return _format_checklist(data)


@mcp.tool()
def task_progress(project: str = "") -> str:
    """Show a compact progress summary of all tasks (or filtered by project)."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        stats = _task_service(conn).task_progress(project=project)
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    bar_len = 20
    filled = round(bar_len * stats["progress_pct"] / 100)
    bar = "#" * filled + "-" * (bar_len - filled)
    lines = [
        "sonder task progress%s" % (" [%s]" % project if project else ""),
        "  [%s] %.1f%%" % (bar, stats["progress_pct"]),
        "  total: %d | pending: %d | in_progress: %d | blocked: %d | done: %d | canceled: %d"
        % (stats["total"], stats["pending"], stats["in_progress"],
           stats["blocked"], stats["done"], stats["canceled"]),
    ]
    return "\n".join(lines)


@mcp.tool()
def task_depend(
    task_id: str,
    depends_on: str,
    remove: bool = False,
) -> str:
    """Add or remove a dependency between tasks. task_id depends on depends_on."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        service = _task_service(conn)
        if remove:
            result = service.remove_dependency(task_id, depends_on)
            if not result.get("removed"):
                return "no dependency found"
            return "removed dependency: %s no longer depends on %s" % (
                result["task_id"][:8], result["depends_on"][:8]
            )
        else:
            result = service.add_dependency(task_id, depends_on)
            return "dependency added: %s depends on %s" % (
                result["task_id"][:8], result["depends_on"][:8]
            )
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()


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
    embeddings.refresh_runtime_revision()
    apply = apply is True
    conn = _open_db()
    try:
        plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=apply)
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
    apply = apply is True
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
    """Refresh missing, legacy, or incompatible vectors with the local model."""
    _maybe_live_reload()
    apply = apply is True
    approved_model = embeddings.EMBED_MODEL
    approved_base = embeddings.BASE
    embed_fn = embeddings.embed
    if _is_cloud_model_name(approved_model):
        return (
            "ERROR: embedding backfill requires a local model; configured model "
            "%r looks cloud-hosted." % approved_model
        )
    if not embeddings.endpoint_is_loopback(approved_base):
        return (
            "ERROR: embedding refresh is local-only; configured Ollama endpoint "
            "is not loopback."
        )
    limit = _safe_limit(limit, 25, 100)
    conn = _open_db()
    updated = 0
    failed = []
    conflicted = []
    target_dimension = embeddings.EXPECTED_DIMENSION
    target_model = embeddings.canonical_model_name(approved_model)
    target_revision = embeddings.current_revision(
        model=approved_model, base=approved_base,
    )
    revision_changed = False
    try:
        if apply:
            probe = embed_fn(
                "Sonder embedding compatibility probe", timeout=30,
                base=approved_base, model=approved_model,
            )
            if not embeddings.valid_vector(probe):
                return "ERROR: embedding refresh could not probe the configured local model."
            if embeddings.current_revision(
                model=approved_model, base=approved_base,
            ) != target_revision:
                return "ERROR: embedding model revision changed during compatibility probe."
            probed_dimension = len(probe)
            if (
                target_dimension is not None
                and probed_dimension != target_dimension
            ):
                return (
                    "ERROR: local embedding probe dimension %d does not match "
                    "the configured/known dimension %d; update SONDER_EMBED_DIM "
                    "or the model configuration before refreshing stored text."
                    % (probed_dimension, target_dimension)
                )
            target_dimension = probed_dimension
        rows = memory_store.lessons_needing_embedding_refresh(
            conn,
            target_model,
            revision=target_revision,
            dimension=target_dimension,
            limit=limit,
        )
        if apply:
            for row in rows:
                try:
                    if embeddings.current_revision(
                        model=approved_model, base=approved_base,
                    ) != target_revision:
                        failed.append(row["id"])
                        revision_changed = True
                        break
                    vector = embed_fn(
                        row.get("text") or "", timeout=30,
                        base=approved_base, model=approved_model,
                    )
                    if (
                        not embeddings.valid_vector(vector)
                        or len(vector) != target_dimension
                    ):
                        failed.append(row["id"])
                        continue
                    blob = embeddings.to_blob(vector)
                    if memory_store.refresh_lesson_embedding(
                        conn,
                        row["id"],
                        blob,
                        target_model,
                        revision=target_revision,
                        dimension=target_dimension,
                        expected=row,
                    ):
                        updated += 1
                    else:
                        conflicted.append(row["id"])
                except (OSError, TypeError, ValueError, OverflowError):
                    failed.append(row["id"])
        remaining = memory_store.count_lessons_needing_embedding_refresh(
            conn,
            target_model,
            revision=target_revision,
            dimension=target_dimension,
        )
    finally:
        conn.close()
    lines = [
        "memory embedding backfill",
        "  mode: %s | local model: %s" % (
            "apply" if apply else "dry-run", approved_model,
        ),
        "  target dimension: %s" % (
            target_dimension if target_dimension else "unknown until apply probe",
        ),
        "  selected stale/missing: %d | updated: %d | conflicted: %d | failed: %d | remaining: %d" % (
            len(rows), updated, len(conflicted), len(failed), remaining,
        ),
    ]
    if rows:
        lines.append("  lesson IDs: %s" % ", ".join(row["id"] for row in rows))
    if failed:
        lines.append("  failed IDs: %s" % ", ".join(failed))
    if conflicted:
        lines.append("  skipped concurrently changed IDs: %s" % ", ".join(conflicted))
    if revision_changed:
        lines.append("  model revision changed during refresh; rerun the batch.")
    if not apply and rows:
        lines.append(
            "  dry-run only; apply probes the live local model, then refreshes "
            "missing or incompatible vectors."
        )
    return "\n".join(lines)


@mcp.tool()
def memory_interaction_embedding_backfill(
    limit: int = 25, apply: bool = False,
) -> str:
    """Refresh raw interaction task vectors with the local embedding model."""
    _maybe_live_reload()
    apply = apply is True
    approved_model = embeddings.EMBED_MODEL
    approved_base = embeddings.BASE
    embed_fn = embeddings.embed
    if _is_cloud_model_name(approved_model):
        return (
            "ERROR: interaction embedding backfill requires a local model; "
            "configured model %r looks cloud-hosted." % approved_model
        )
    if not embeddings.endpoint_is_loopback(approved_base):
        return (
            "ERROR: interaction embedding refresh is local-only; configured "
            "Ollama endpoint is not loopback."
        )
    limit = _safe_limit(limit, 25, 100)
    conn = _open_db()
    updated = 0
    failed = []
    conflicted = []
    target_dimension = embeddings.EXPECTED_DIMENSION
    target_model = embeddings.canonical_model_name(approved_model)
    target_revision = embeddings.current_revision(
        model=approved_model, base=approved_base,
    )
    revision_changed = False
    try:
        if apply:
            probe = embed_fn(
                "Sonder interaction embedding compatibility probe", timeout=30,
                base=approved_base, model=approved_model,
            )
            if not embeddings.valid_vector(probe):
                return (
                    "ERROR: interaction embedding refresh could not probe the "
                    "configured local model."
                )
            if embeddings.current_revision(
                model=approved_model, base=approved_base,
            ) != target_revision:
                return (
                    "ERROR: interaction embedding model revision changed during "
                    "compatibility probe."
                )
            probed_dimension = len(probe)
            if (
                target_dimension is not None
                and probed_dimension != target_dimension
            ):
                return (
                    "ERROR: local interaction embedding probe dimension %d does "
                    "not match the configured/known dimension %d; update "
                    "SONDER_EMBED_DIM or the model configuration before "
                    "refreshing stored task text."
                    % (probed_dimension, target_dimension)
                )
            target_dimension = probed_dimension
        rows = memory_store.interactions_needing_task_embedding_refresh(
            conn,
            target_model,
            revision=target_revision,
            dimension=target_dimension,
            limit=limit,
        )
        if apply:
            for row in rows:
                try:
                    if embeddings.current_revision(
                        model=approved_model, base=approved_base,
                    ) != target_revision:
                        failed.append(row["id"])
                        revision_changed = True
                        break
                    vector = embed_fn(
                        row.get("task") or "", timeout=30,
                        base=approved_base, model=approved_model,
                    )
                    if (
                        not embeddings.valid_vector(vector)
                        or len(vector) != target_dimension
                    ):
                        failed.append(row["id"])
                        continue
                    if memory_store.refresh_interaction_task_embedding(
                        conn,
                        row["id"],
                        embeddings.to_blob(vector),
                        target_model,
                        revision=target_revision,
                        dimension=target_dimension,
                        expected=row,
                    ):
                        updated += 1
                    else:
                        conflicted.append(row["id"])
                except (OSError, TypeError, ValueError, OverflowError):
                    failed.append(row["id"])
        remaining = (
            memory_store.count_interactions_needing_task_embedding_refresh(
                conn,
                target_model,
                revision=target_revision,
                dimension=target_dimension,
            )
        )
    finally:
        conn.close()
    lines = [
        "memory interaction embedding backfill",
        "  mode: %s | local model: %s" % (
            "apply" if apply else "dry-run", approved_model,
        ),
        "  target dimension: %s" % (
            target_dimension if target_dimension else "unknown until apply probe",
        ),
        "  selected stale/missing: %d | updated: %d | conflicted: %d | "
        "failed: %d | remaining: %d" % (
            len(rows), updated, len(conflicted), len(failed), remaining,
        ),
    ]
    if rows:
        lines.append(
            "  interaction IDs: %s" % ", ".join(row["id"] for row in rows)
        )
    if failed:
        lines.append("  failed IDs: %s" % ", ".join(failed))
    if conflicted:
        lines.append(
            "  skipped concurrently changed IDs: %s" % ", ".join(conflicted)
        )
    if revision_changed:
        lines.append("  model revision changed during refresh; rerun the batch.")
    if not apply and rows:
        lines.append(
            "  dry-run only; apply probes the live local model, then refreshes "
            "bounded task vectors from their locally stored task text."
        )
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
        autopilot = _application().automation.snapshot(include_finished=False, limit=100)
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
    stale_embeddings = (
        quality.get("embedding_legacy", 0)
        + quality.get("embedding_model_mismatch", 0)
        + quality.get("embedding_revision_mismatch", 0)
        + quality.get("embedding_dimension_missing", 0)
        + quality.get("embedding_dimension_invalid", 0)
    )
    if stale_embeddings:
        add(
            "memory",
            "medium",
            "%d lesson embeddings lack current provenance or are incompatible."
            % stale_embeddings,
            "Run memory_embedding_backfill(apply=False), then refresh bounded "
            "batches locally with apply=True.",
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
        provenance = mcp_state.get("provenance") or {}
        add(
            "runtime",
            "high",
            (
                "The MCP process is attached to a stale runtime source root."
                if provenance.get("issue") == "stale_source_root"
                else "The latest MCP source refresh failed closed."
            ),
            _safe_mcp_recovery_action(provenance) or (
                "Run /mcp status, fix the reported source error, then use "
                "/mcp refresh; the last known-good tools remain active."
            ),
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
    # The blended positive rate is dominated by autograded outcomes -- the
    # runtime setting and marking its own curriculum -- and hides the number an
    # operator actually needs: how often work a caller delegated was judged
    # good. learning_health_status separates these; this report used to blend
    # them, so it could show 96% positive and 100/100 readiness while
    # caller-judged work sat near 53% and the health view said "watch".
    reviewed = learning_state.get("reviewed_outcomes", 0)
    reviewed_positive = learning_state.get("reviewed_positive_percent", 0.0)
    if reviewed >= 30 and reviewed_positive < 60.0:
        add(
            "learning",
            "medium",
            "Caller-judged work succeeds %.1f%% of the time (%d reviewed outcome(s))."
            % (reviewed_positive, reviewed),
            "This is the honest hit rate; the blended figure is inflated by "
            "autograded self-marking. Review roughly half of delegated output, "
            "and record negatives -- record_outcome with a failing signal is "
            "what the store is starved of.",
        )
    elif reviewed < 30 and outcomes >= 200:
        add(
            "learning",
            "low",
            "Almost no outcomes have been judged by a caller (%d of %d)."
            % (reviewed, outcomes),
            "Autograded outcomes cannot tell you whether delegated work is any "
            "good. Use record_outcome after real use so the reviewed rate means "
            "something.",
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
        "reviewed_outcomes": learning_state.get("reviewed_outcomes", 0),
        "reviewed_positive_percent": learning_state.get("reviewed_positive_percent", 0.0),
        "autograded_outcomes": learning_state.get("autograded_outcomes", 0),
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
        "  learning: %s interactions, %s outcomes, %s%% covered" % (
            report.get("interactions", 0),
            report.get("outcomes", 0),
            report.get("learning_health", {}).get("outcome_coverage_percent", 0),
        ),
        # Never show the blended rate alone: it is dominated by the runtime
        # marking its own curriculum, and reads as a quality score when it
        # is not one.
        "    caller-judged: %s%% of %s reviewed | autograded: %s%% of %s | blended: %s%%" % (
            report.get("reviewed_positive_percent", 0),
            report.get("reviewed_outcomes", 0),
            report.get("learning_health", {}).get("autograded_positive_percent", 0),
            report.get("autograded_outcomes", 0),
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
            return _offload_impl(
                prompt=prompt,
                tier=tier,
                temperature=0.2,
                num_predict=1400,
                learn=learn,
                timeout=timeout,
                cancel_check=master_orchestrator.current_worker_cancel_requested,
            )
    return worker


def _orchestrator_agent_worker(
    tier: str, project: str, max_steps: int = 8,
):
    response_id = activity_tracker.current_response_id()
    project_scope = master_orchestrator.resolve_repository_project_root("", project)

    def worker(prompt: str, assigned_project: str) -> master_orchestrator.RepositoryWorkerResult:
        with activity_tracker.bind_response(response_id):
            assigned = master_orchestrator.resolve_repository_project_root(
                prompt, assigned_project,
            )
            if not master_orchestrator.same_project_root(assigned, project_scope):
                raise RuntimeError("repository worker assignment changed after fleet start")
            # The host-bound project is passed directly; child prompt parsing is
            # only an ambiguity check and can never select the process cwd.
            receipt = _agent_impl(
                prompt,
                tier=tier,
                max_steps=max_steps,
                allow_web=False,
                require_file_evidence=True,
                read_only=True,
                include_evidence=True,
                auto_checklist=True,
                project=project_scope,
                return_host_receipt=True,
                cancel_check=master_orchestrator.current_worker_cancel_requested,
            )
            return master_orchestrator.repository_worker_result(
                receipt, project_scope,
            )
    return worker


def _master_scope_error(detail) -> str:
    return "%s\nScope error: %s" % (
        master_orchestrator.EVIDENCE_REQUIRED, str(detail or "unknown scope error"),
    )


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
    agents: int = 0,
    worker_cap: int = 0,
    tier: str = "auto",
    learn: bool = False,
    retry_of: str = "",
    project: str = "",
) -> str:
    """Run a master pass inline or with hardware-scheduled delegated agents.

    mode="ask" returns the choice prompt. mode="inline" keeps work in the master
    lane. mode="delegate" queues bounded subagents across RAM/CPU-safe worker
    slots, then audits and merges their outputs. mode="fleet" queues the full
    hardware-derived breadth ceiling in the background and returns immediately.
    Pass a positive ``agents`` value to set fleet breadth. ``worker_cap`` is a
    per-run opt-in that may raise concurrent slots above the hardware-derived
    default, but never above the operator/compiled ceiling. A clear task phrase
    such as "use 24 workers" fills both values when they are omitted.
    For existing repository work, pass ``project`` as an existing root. Every
    child and aggregate is confined to that canonical root; missing or
    conflicting repository scope fails closed instead of inheriting the cwd.
    Status is visible through master_status().
    """
    _maybe_live_reload()
    task = (task or "").strip()
    try:
        protected_objectives = master_orchestrator.fleet_provenance.parse_objectives(task)
    except master_orchestrator.fleet_provenance.ProvenanceError as exc:
        return "ERROR: invalid fleet objective contract: %s" % exc
    inferred_worker_cap = master_orchestrator.requested_worker_cap(task)
    raw_worker_cap = worker_cap
    if (worker_cap is None or worker_cap == 0) and not isinstance(worker_cap, bool):
        worker_cap = inferred_worker_cap
    worker_cap_supplied = worker_cap is not None and worker_cap != 0
    if worker_cap_supplied:
        cap_probe = master_orchestrator.capacity(agents or None, worker_cap=worker_cap)
        if cap_probe.get("worker_cap_error"):
            return "ERROR: invalid worker_cap: %s" % cap_probe["worker_cap_error"]
        worker_cap = int(cap_probe["requested_worker_cap"])
        if not agents:
            agents = worker_cap
    elif isinstance(raw_worker_cap, bool):
        return "ERROR: invalid worker_cap: boolean values are not worker counts"
    else:
        worker_cap = None
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
    if mode in ("ask", "choose", "prompt"):
        if worker_cap:
            delegate_count = fleet_count = min(
                int(agents or worker_cap), master_orchestrator.explicit_agent_ceiling(),
            )
        else:
            delegate_count = master_orchestrator.clamp_agent_count(agents, default=3)
            fleet_count = master_orchestrator.clamp_agent_count(
                agents, default=master_orchestrator.max_agents(),
            )
        if worker_cap:
            delegate_capacity = master_orchestrator.capacity(delegate_count, worker_cap=worker_cap)
            fleet_capacity = master_orchestrator.capacity(fleet_count, worker_cap=worker_cap)
        else:
            delegate_capacity = master_orchestrator.capacity(delegate_count)
            fleet_capacity = master_orchestrator.capacity(fleet_count)
        return (
            "Master orchestrator ready.\n"
            "Choose execution mode:\n"
            "  inline   - master handles the task directly.\n"
            "  delegate - queue %d agent(s) across %d safe worker slot(s), audit, then merge.\n"
            "  fleet    - queue %d agent(s) across %d safe worker slot(s), return immediately, then monitor it.\n"
            "              Omit agents (or pass 0) to use the hardware ceiling.\n"
            "              Set worker_cap (or say 'use N workers') for a bounded per-run override.\n"
            "Keywords fleet, swarm, spawn as many agents, parallel agents, and\n"
            "parallel workflow select fleet automatically without replacing an explicit agent count.\n"
            "Call master_orchestrate(task, mode='inline'|'delegate'|'fleet') or chat `/master inline ...`."
        ) % (
            delegate_count,
            delegate_capacity["worker_slots"],
            fleet_count,
            fleet_capacity["worker_slots"],
        )
    if not task:
        return "ERROR: empty task."
    tier = _runtime_lane_tier("fleet", tier)
    audit_tier = _runtime_lane_tier("review")
    # A task that needs repository/filesystem inspection is by definition not
    # a greenfield build request (creative_router's own docstring: "Conservative
    # routing for explicit greenfield game/artifact build requests") -- check
    # this FIRST so ordinary code-analysis tasks (e.g. "generate a summary of
    # these files") never get misclassified into the asset/game forge pipeline
    # just because they contain a common verb+noun pair the regex also uses
    # for creative intent (generate/create/build + model/document/diagram/...).
    raw_project = str(project or "").strip()
    explicit_project = master_orchestrator.canonical_project_root(raw_project)
    project_requested = bool(raw_project and raw_project.lower() not in {"none", "default"})
    if project_requested and not explicit_project:
        return _master_scope_error(
            "project must name an existing directory or source file"
        )
    needs_repo_tools = (
        bool(protected_objectives)
        or bool(explicit_project)
        or master_orchestrator.requires_repository_tools(task)
    )
    if protected_objectives and (
        _is_cloud_tier(tier) or _is_cloud_tier(audit_tier)
    ):
        return (
            "ERROR: protected fleet objective contracts require local worker "
            "and audit tiers so repository evidence cannot leave the host."
        )
    project_scope = ""
    if needs_repo_tools:
        try:
            project_scope = master_orchestrator.resolve_repository_project_root(
                task, project,
            )
        except ValueError as exc:
            return _master_scope_error(exc)
    creative_intent = None if needs_repo_tools else creative_router.classify(task, mode=mode)
    if creative_intent:
        return _master_grounded_build(
            task, mode, tier, creative_intent, retry_of=retry_of,
        )
    worker = (
        _orchestrator_agent_worker(tier, project_scope)
        if needs_repo_tools
        else _orchestrator_worker(
            tier,
            # Protected objective runs are never captured as learnable
            # interactions. This guarantees a drifted result cannot later be
            # promoted through record_outcome, even after a restart.
            learn=learn and not protected_objectives,
            timeout=_master_timeout("SONDER_MASTER_AGENT_TIMEOUT", 150),
        )
    )
    if mode in ("inline", "master"):
        result = master_orchestrator.run_inline(
            task,
            worker,
            metadata={
                "tier": tier, "mode": "inline", "retry_of": retry_of,
                "project": project_scope,
            },
            project=project_scope,
        )
        return result["output"]
    if mode in ("delegate", "delegated", "agents", "parallel", "fleet", "swarm", "fanout"):
        run_fleet_in_background = mode in ("fleet", "swarm", "fanout")
        if run_fleet_in_background and not worker_cap:
            agents = master_orchestrator.clamp_agent_count(
                agents, default=master_orchestrator.max_agents(),
            )
        runner = (
            master_orchestrator.start_delegated
            if run_fleet_in_background
            else master_orchestrator.run_delegated
        )
        result = runner(
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
                "project": project_scope,
            },
            project=project_scope,
            worker_cap=worker_cap,
        )
        if run_fleet_in_background:
            return "\n".join([
                "master orchestration started",
                "mode: fleet | master=%s | agents=%d" % (
                    result["master_id"], len(result.get("agents") or []),
                ),
                "worker slots: %d (bounded concurrent model calls)" % (
                    result.get("worker_slots", 1)
                ),
                "monitor: master_status() | cancel: master_cancel('%s')" % (
                    result["master_id"]
                ),
            ])
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


def execution_status_data(
    agent_snapshot: dict | None = None,
    activity_snapshot: dict | None = None,
    *,
    include_detail: bool | None = None,
) -> dict:
    """Return the shared terminal/app fleet concurrency contract."""
    try:
        snapshot = agent_snapshot
        if snapshot is None:
            snapshot = master_orchestrator.snapshot(include_finished=False, limit=1)
        activity = activity_snapshot
        if activity is None:
            activity = activity_tracker.snapshot()
        feed = activity_tracker.execution_feed(
            activity, include_detail=include_detail,
        )
        return execution_status.with_feed(snapshot, feed)
    except Exception as exc:
        return execution_status.with_feed(
            execution_status.unavailable(type(exc).__name__), None,
        )


def execution_feed_data(activity_snapshot: dict | None = None) -> dict:
    """Return only the projected feed, without fleet capacity probes."""
    try:
        activity = activity_snapshot
        if activity is None:
            activity = activity_tracker.snapshot()
        return activity_tracker.execution_feed(activity)
    except Exception as exc:
        return activity_tracker.execution_feed([]) | {
            "error": type(exc).__name__,
        }


@mcp.tool()
def master_capacity(requested_agents: int = 0, worker_cap: int = 0) -> str:
    """Show default or explicit per-run bounded orchestration capacity."""
    _maybe_live_reload()
    try:
        value = int(requested_agents or 0)
    except (TypeError, ValueError):
        value = 0
    requested = value if value > 0 else None
    data = (
        master_orchestrator.capacity(requested, worker_cap=worker_cap)
        if worker_cap else master_orchestrator.capacity(requested)
    )
    return master_orchestrator.format_capacity(data)


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
    if status not in ("interrupted", "failed", "task_drift", "cancelled"):
        return "ERROR: master %s is %s; only interrupted/failed/cancelled work or task_drift work can be retried." % (
            candidate["id"], status or "unknown",
        )
    task = (candidate.get("task") or "").strip()
    if not task:
        return "ERROR: persisted master %s has no recoverable task text." % candidate["id"]
    try:
        objectives = master_orchestrator.fleet_provenance.parse_objectives(task)
    except master_orchestrator.fleet_provenance.ProvenanceError as exc:
        return "ERROR: persisted master provenance is invalid: %s" % exc
    stored_digest = str(candidate.get("master_task_digest") or "")
    recovered_digest = master_orchestrator.fleet_provenance.task_digest(task)
    stored_objective_ids = list(candidate.get("objective_ids") or ())
    recovered_objective_ids = [
        objective.objective_id for objective in objectives
    ]
    if stored_digest and stored_digest != recovered_digest:
        return (
            "ERROR: persisted master task text no longer matches its immutable digest; "
            "start a fresh orchestration instead of retrying changed work."
        )
    if stored_objective_ids != recovered_objective_ids:
        return (
            "ERROR: persisted master objective IDs no longer match the task contract; "
            "start a fresh orchestration instead of retrying ambiguous work."
        )
    if objectives and not stored_digest:
        return (
            "ERROR: protected legacy master has no immutable task digest; start a "
            "fresh orchestration instead of retrying unverifiable work."
        )
    mode = (candidate.get("mode") or "delegated").lower()
    if mode not in (
        "inline", "master", "delegate", "delegated", "agents", "parallel",
        "fleet", "swarm", "fanout",
    ):
        mode = "delegated"
    agents = int(candidate.get("requested_agents") or 3)
    retry_tier = str(tier or "code").strip() or "code"
    retry_kwargs = {
        "task": task, "mode": mode, "agents": agents, "tier": retry_tier,
        "learn": False, "retry_of": candidate["id"],
    }
    retry_project = str(candidate.get("project") or "").strip()
    if not retry_project:
        # Compatibility for masters created by a still-running pre-project
        # fleet_store module.  New orchestrators mirror the canonical project
        # root into files_json, which the old persistence path already knows
        # how to save, until live reload reaches fleet_store itself.
        for persisted_path in candidate.get("files") or ():
            value = str(persisted_path or "").strip()
            if value and os.path.isdir(value):
                retry_project = value
                break
    if retry_project:
        retry_kwargs["project"] = retry_project
    result = master_orchestrate(**retry_kwargs)
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
    # Was `if token:` -- which only checked a token that was volunteered, so
    # omitting it skipped the gate entirely. Same fail-open shape as the one
    # flagged in turn_inspect; this is where that pattern was copied from.
    refusal = _developer_gate("debug_inspect", token, None)
    if refusal:
        return refusal
    sections = [
        "sonder debug inspect",
        "  note: private hidden chain-of-thought is not exposed; use trace/tool/activity logs instead.",
        "",
        admin_status(token),
        "",
        format_mcp_runtime(),
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


_TRUSTED_REPOSITORY_APPROVAL = object()


def _file_bypass_allowed(token: str = "", approval: str = "") -> bool:
    if unsafe_lab.active():
        # The exact acknowledgement substitutes for every model-visible file
        # approval only in this deliberately unrestricted process.
        return True
    # Repository agents may receive one host-authorized project root.  The
    # unforgeable in-process sentinel is injected only after the read-only
    # policy has validated the tool and path; an MCP caller can supply strings,
    # but can never manufacture this object identity.
    if approval is _TRUSTED_REPOSITORY_APPROVAL:
        return True
    if file_ops.bypass_enabled():
        return True
    expected = os.environ.get("SONDER_FILE_APPROVAL_CODE", "").strip()
    if expected and approval and approval == expected:
        return True
    return _file_developer_allowed(token)


_GIT_IGNORE_DISCOVERY_TOOLS = frozenset({
    "workspace_inventory", "directory_tree", "file_find", "text_search",
    "script_search",
})


def _include_ignored_error(tool_name: str, include_ignored, token: str = "") -> str:
    if not include_ignored:
        return ""
    if _file_developer_allowed(token):
        return ""
    return (
        "ERROR: include_ignored=true for '%s' requires an explicitly "
        "authenticated developer account." % tool_name
    )


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
    return _task_service(conn).checklist(checklist_id).to_dict()


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
        normalized_items = task_use_cases.normalize_checklist_items(items_json)
        conn = _open_db()
        try:
            service = _task_service(conn)
            checklist = service.create_checklist(
                title, [
                    {"title": item_title, "detail": detail}
                    for item_title, detail in normalized_items
                ], project=project, owner=owner, priority=priority,
            )
            data = checklist.to_dict()
        finally:
            conn.close()
    except Exception as exc:
        _record_direct_tool("checklist_create", {"title": title}, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    _record_direct_tool(
        "checklist_create", {"title": title, "items": len(checklist.items)},
        ok=True, started=started, summary=data["summary"],
    )
    service.publish_checklist(checklist)
    return _format_checklist(data)


@mcp.tool()
def checklist_show(checklist_id: str) -> str:
    """Show a checklist with Codex-style pending/active/done markers."""
    _maybe_live_reload()
    started = time.time()
    try:
        conn = _open_db()
        try:
            service = _task_service(conn)
            checklist = service.checklist(checklist_id)
            data = checklist.to_dict()
        finally:
            conn.close()
    except Exception as exc:
        _record_direct_tool(
            "checklist_show", {"checklist_id": checklist_id},
            ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    service.publish_checklist(checklist)
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
            service = _task_service(conn)
            checklist = service.update_checklist(checklist_id, item, status, note)
            data = checklist.to_dict()
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
    service.publish_checklist(checklist)
    return _format_checklist(data)


def _record_file_activity(
    default_action: str,
    data: dict,
    *,
    preview=None,
    preview_kind: str = "",
) -> None:
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
        preview=preview,
        preview_kind=preview_kind,
    )


_INTERACTION_ID_RE = re.compile(r"\[interaction_id:\s*([0-9A-Za-z_-]+)\]")


def _record_outcome_signal(interaction_id: str, signal: str) -> None:
    """Write one grounded outcome row, bypassing the model-facing wrapper."""
    conn = _open_db()
    try:
        memory_store.record_outcome_row(
            conn, interaction_id, signal, reward.score(signal),
        )
    finally:
        conn.close()


def _feed_grounded_outcome(name, ok, output, args=None) -> None:
    """Attribute execution evidence to the work it judges.

    The outcome store holds ~9,000 rows and only ~190 of them measure delegated
    work, because filing an outcome is a manual step and people file successes
    far more readily than failures. The verification tools already know the
    truth, so take it from them instead of asking anyone to remember.
    """
    project = ""
    if isinstance(args, dict):
        project = str(args.get("project") or args.get("root") or "")
    try:
        if name in grounded_outcomes.GENERATORS:
            match = _INTERACTION_ID_RE.search(str(output or ""))
            if match:
                grounded_outcomes.note_generation(match.group(1), name, project)
        elif name in grounded_outcomes.VERIFIERS:
            grounded_outcomes.attribute(
                name, bool(ok), project, record_fn=_record_outcome_signal,
            )
    except Exception:
        # Bookkeeping must never break the run it is observing.
        pass


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
    _feed_grounded_outcome(name, ok, output, args)


@mcp.tool()
def turn_inspect(index: int = 0, full_prompt: bool = False, token: str = "") -> str:
    """Show what actually went into a recent turn: prompt, lessons, tier, model.

    index 0 is the most recent turn, 1 the one before it, and so on. This is
    the retrospective half of /trace: the same pipeline state, for turns that
    already ran, so an intermittent result can be debugged without first
    reproducing it with tracing switched on.

    Developer-gated like debug_inspect, because the captured prompts are the
    caller's own text.
    """
    _maybe_live_reload()
    started = time.time()
    refusal = _developer_gate("turn_inspect", token, started)
    if refusal:
        return refusal
    turns = list(_TURN_TRACES)
    if not turns:
        _record_direct_tool("turn_inspect", {}, ok=True, started=started)
        return (
            "no turns captured yet.\n"
            "  The buffer fills as answers are generated and holds the last %d.\n"
            "  It lives in memory only and is empty after a restart."
            % _TURN_TRACES.maxlen
        )
    try:
        position = int(index)
    except (TypeError, ValueError):
        position = 0
    if position < 0 or position >= len(turns):
        _record_direct_tool("turn_inspect", {}, ok=False, started=started)
        return "no turn at index %s; %d captured (0 is most recent)." % (
            index, len(turns),
        )
    turn = turns[-1 - position]
    prompt_text = turn["augmented_prompt"]
    if not full_prompt and len(prompt_text) > 2000:
        prompt_text = prompt_text[:2000] + (
            "\n... (%d more chars; pass full_prompt=True)" % (
                len(turn["augmented_prompt"]) - 2000)
        )
    lines = [
        "turn -%d of %d captured   %s" % (position, len(turns), turn["ts"]),
        "  model: %s   tier: %s%s" % (
            turn["model"], turn["tier"],
            "   interaction: %s" % turn["interaction_id"]
            if turn["interaction_id"] else "",
        ),
        "",
        "  asked:",
    ]
    lines += ["    " + line for line in (turn["prompt"] or "(none)").splitlines()[:20]]
    lines += ["", "  lessons retrieved: %d" % len(turn["lessons"])]
    lines += ["    - " + text for text in turn["lessons"]]
    lines += ["", "  exact prompt sent to the model:"]
    lines += ["    " + line for line in (prompt_text or "(none)").splitlines()]
    lines += ["", "  response began:"]
    lines += ["    " + line for line in
              (turn["response_head"] or "(none)").splitlines()[:12]]
    output = "\n".join(lines)
    _record_direct_tool(
        "turn_inspect", {"index": position}, ok=True, started=started,
    )
    return output


@mcp.tool()
def reasoning_show(token: str = "") -> str:
    """Show the reasoning the model emitted for the current/last turn, if enabled.

    Two gates, both of which must pass. The operator gate
    (SONDER_EXPOSE_REASONING) decides whether reasoning is captured at all --
    with it off Sonder never asks the model for its thinking. The caller gate
    decides who may read it: on a deployment that authenticates callers this
    needs a developer token, because the reasoning belongs to whoever's turn
    produced it. The HTTP path gates the same way via SONDER_REASONING_AUDIENCE.

    This is NOT admin_private_chain_of_thought. That refuses arbitrary
    inspection of hidden reasoning and still does. This shows only what a
    reasoning model deliberately emitted, for the turn you just ran, on the
    channel it emits separately from its answer.
    """
    _maybe_live_reload()
    started = time.time()
    refusal = _developer_gate("reasoning_show", token, started)
    if refusal:
        return refusal
    if not reasoning_exposure_enabled():
        _record_direct_tool("reasoning_show", {}, ok=True, started=started)
        return (
            "reasoning is not exposed.\n"
            "  SONDER_EXPOSE_REASONING is off, so Sonder does not request the\n"
            "  model's thinking at all -- there is nothing withheld, there is\n"
            "  nothing captured.\n\n"
            "  enable:   set SONDER_EXPOSE_REASONING=1  (then restart the runtime)\n"
            "  audience: SONDER_REASONING_AUDIENCE=developer (default) | all\n\n"
            "  Unrelated to /cot, which refuses arbitrary hidden-state\n"
            "  inspection and stays refused either way."
        )
    record = activity_tracker.current_reasoning() or activity_tracker.latest_reasoning()
    if not record:
        _record_direct_tool("reasoning_show", {}, ok=True, started=started)
        return (
            "reasoning is enabled, but nothing is recorded for this turn.\n"
            "  Either the last answer came from a model that emits no separate\n"
            "  thinking channel, or no turn has run since the runtime started."
        )
    text = str(record.get("text") or "").strip()
    model = str(record.get("model") or "")
    lines = ["model reasoning%s" % (" (%s)" % model if model else ""), ""]
    lines += ["  " + line for line in (text or "(empty)").splitlines()]
    output = "\n".join(lines)
    _record_direct_tool("reasoning_show", {}, ok=True, started=started)
    return output


@mcp.tool()
def calibration_status() -> str:
    """Measured reliability, split by population and never averaged.

    Reports how often delegated work was judged good by a caller, separately
    from how often generated code built or passed tests. A single figure over
    both reads like accuracy and is not one: the self-graded population is
    ~50x larger and ~45 points higher, so combining them hides exactly the
    number worth knowing.
    """
    _maybe_live_reload()
    started = time.time()
    conn = _open_db()
    try:
        output = calibration.report(conn)
    finally:
        conn.close()
    _record_direct_tool("calibration_status", {}, ok=True, started=started)
    return output


@mcp.tool()
def permission_mode(mode: str = "", explain: bool = False) -> str:
    """Show or set how much Sonder does without asking.

    Modes, least to most autonomous: plan (reads only), manual (ask before
    anything that is not a read), acceptEdits (file changes proceed, running
    programs still asks), auto (programs proceed too). Destructive tools ask in
    every mode, including auto.

    Elevation is a separate axis no mode grants; see permission_policy.
    """
    _maybe_live_reload()
    started = time.time()
    wanted = str(mode or "").strip()
    try:
        if not wanted:
            output = permission_modes.overview()
        elif explain:
            output = permission_modes.describe(wanted)
        else:
            permission_modes.set_mode(wanted)
            output = permission_modes.describe()
    except ValueError as exc:
        _record_direct_tool(
            "permission_mode", {"mode": wanted}, ok=False, started=started,
            summary=str(exc),
        )
        return str(exc)
    _record_direct_tool("permission_mode", {"mode": wanted}, ok=True, started=started)
    return output


def permission_mode_data() -> dict:
    """Mode state as structured data, for the HTTP API and the app."""
    active = permission_modes.current_mode()
    return {
        "mode": active,
        "label": permission_modes.MODE_LABELS.get(active, active),
        "blurb": permission_modes.MODE_BLURBS.get(active, ""),
        "elevated": permission_modes.elevated(),
        "elevationReason": permission_modes.elevation_reason(),
        "modes": [
            {
                "name": name,
                "label": permission_modes.MODE_LABELS.get(name, name),
                "blurb": permission_modes.MODE_BLURBS.get(name, ""),
            }
            for name in permission_modes.MODES
        ],
        "matrix": dict(permission_modes._MATRIX[active]),
    }


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
    include_ignored: bool = False,
) -> str:
    """Find files under allowed roots; ignored paths require developer authentication."""
    _maybe_live_reload()
    started = time.time()
    policy_error = _include_ignored_error("file_find", include_ignored, token)
    if policy_error:
        return policy_error
    try:
        data = file_ops.find_files(
            query=query,
            root=root,
            max_results=max_results,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            include_ignored=include_ignored,
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
    count = len(data["results"])
    header = "file find: %s under %s" % (data["query"], data["root"])
    if data.get("truncated"):
        # Make the cap explicit so a counting caller does not read N as a total.
        header += " (TRUNCATED at %d — more matches exist; raise max_results for a full count)" % data.get("limit", count)
    else:
        header += " (%d match(es))" % count
    lines = [header]
    for row in data["results"]:
        lines.append("  %(type)s %(relative)s (%(bytes)s bytes)" % row)
    if not data["results"]:
        lines.append("  (no matches)")
    return "\n".join(lines)


@mcp.tool()
def repository_symbol_index(
    path: str = ".",
    glob: str = "*",
    language: str = "",
    max_files: int = 200,
    max_total_bytes: int = 2_000_000,
    max_file_bytes: int = 256_000,
    max_symbols: int = 2_000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Build a bounded read-only declaration index inside one guarded repository.

    Uses Python AST and conservative stdlib-only extraction for JavaScript,
    TypeScript, C, C++, C#, Rust, and Go. It never executes repository content,
    follows symlinks, shells out, or accesses the network.
    """
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "glob": glob, "language": language,
        "max_files": max_files, "max_total_bytes": max_total_bytes,
        "max_file_bytes": max_file_bytes, "max_symbols": max_symbols,
    }
    try:
        trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
        data = symbol_index.index_repository(
            path=path, glob=glob, language=language, max_files=max_files,
            max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
            max_symbols=max_symbols, extra_roots=trusted_roots,
        )
    except Exception as exc:
        _record_direct_tool(
            "repository_symbol_index", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    summary = "%d file(s), %d symbol(s)%s" % (
        data["files"], len(data["symbols"]),
        ", truncated" if data["truncated"] else "",
    )
    _record_direct_tool(
        "repository_symbol_index", args, ok=True, started=started, summary=summary,
    )
    activity_tracker.record_event(
        "repository_symbol_index", summary=summary, path=data["root"],
    )
    return symbol_index.format_index(data)


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
            developer_authorized=_file_developer_allowed(token),
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


_CONTEXT_PACK_MAX_FILES = 64
_CONTEXT_PACK_MAX_TOTAL_BYTES = 1_000_000


def _context_pack_paths(paths_json) -> list[str]:
    """Normalize the public JSON/list shape without resolving any paths."""
    try:
        value = json.loads(paths_json) if isinstance(paths_json, str) else paths_json
    except (TypeError, ValueError) as exc:
        raise ValueError("paths_json must be a JSON array of file paths") from exc
    if not isinstance(value, list):
        raise ValueError("paths_json must be a JSON array of file paths")
    if not value:
        raise ValueError("paths_json must contain at least one file path")
    paths = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("paths_json item %d must be a non-empty string" % (index + 1))
        if "\x00" in item:
            raise ValueError("paths_json item %d contains a NUL byte" % (index + 1))
        paths.append(item.strip())
    return paths


def _context_pack_int(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(ceiling, number))


def _context_pack_utf8_prefix(text: str, max_bytes: int) -> tuple[str, int, bool]:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or ""), len(raw), False
    prefix = raw[:max_bytes]
    # Do not emit half of a multibyte codepoint. The body can therefore be a
    # few bytes below the cap, but never above it.
    decoded = prefix.decode("utf-8", errors="ignore")
    return decoded, len(decoded.encode("utf-8")), True


@mcp.tool()
def context_pack(
    paths_json: str,
    max_files: int = 12,
    max_total_bytes: int = 256000,
    max_bytes_per_file: int = 64000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Read several guarded text files into one deterministic bounded pack.

    paths_json is a JSON array of explicit file paths. Each path independently
    passes the same containment, symlink, sensitive-file, and approval policy
    as file_read. File-body UTF-8 bytes count against max_total_bytes; headers
    do not. Errors are reported per file and do not abort later reads.
    """
    _maybe_live_reload()
    started = time.time()
    args = {
        "max_files": max_files,
        "max_total_bytes": max_total_bytes,
        "max_bytes_per_file": max_bytes_per_file,
    }
    try:
        paths = _context_pack_paths(paths_json)
    except ValueError as exc:
        _record_direct_tool("context_pack", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc

    file_limit = _context_pack_int(max_files, 12, _CONTEXT_PACK_MAX_FILES)
    total_limit = _context_pack_int(
        max_total_bytes, 256000, _CONTEXT_PACK_MAX_TOTAL_BYTES,
    )
    per_file_limit = _context_pack_int(
        max_bytes_per_file, 64000, file_ops.MAX_READ_BYTES,
    )
    selected = paths[:file_limit]
    omitted = max(0, len(paths) - len(selected))
    remaining = total_limit
    emitted = 0
    errors = 0
    truncated_files = 0
    sections = []
    bypass = _file_bypass_allowed(token, approval)
    developer_authorized = _file_developer_allowed(token)

    for index, requested in enumerate(selected, 1):
        display = requested.replace("\r", "\\r").replace("\n", "\\n")
        header = "===== CONTEXT FILE %d/%d: %s =====" % (
            index, len(selected), display,
        )
        if remaining <= 0:
            truncated_files += 1
            sections.append("\n".join([
                header,
                "status: skipped",
                "included-bytes: 0",
                "truncated: true (total byte budget exhausted)",
            ]))
            continue
        read_limit = min(per_file_limit, remaining)
        try:
            data = file_ops.read_file(
                requested,
                max_bytes=read_limit,
                extra_roots=extra_roots,
                bypass=bypass,
                developer_authorized=developer_authorized,
            )
            body, body_bytes, decode_truncated = _context_pack_utf8_prefix(
                data.get("text", ""), read_limit,
            )
            source_bytes = int(data.get("bytes", 0))
            was_truncated = bool(data.get("truncated")) or decode_truncated
            reason = ""
            if was_truncated:
                truncated_files += 1
                reason = (
                    "total byte budget"
                    if read_limit < per_file_limit
                    else "per-file byte cap"
                )
            emitted += body_bytes
            remaining -= body_bytes
            lines = [
                header,
                "status: ok",
                "source-bytes: %d" % source_bytes,
                "included-bytes: %d" % body_bytes,
                "truncated: %s%s" % (
                    "true" if was_truncated else "false",
                    " (%s)" % reason if reason else "",
                ),
                "",
                body,
            ]
            sections.append("\n".join(lines))
        except Exception as exc:
            errors += 1
            sections.append("\n".join([
                header,
                "status: error",
                "included-bytes: 0",
                "truncated: false",
                "error: %s: %s" % (type(exc).__name__, exc),
            ]))

    pack_truncated = bool(omitted or truncated_files)
    summary = (
        "context pack: requested=%d selected=%d emitted-bytes=%d "
        "max-files=%d max-total-bytes=%d max-bytes-per-file=%d errors=%d"
        % (
            len(paths), len(selected), emitted, file_limit, total_limit,
            per_file_limit, errors,
        )
    )
    truncation = "pack-truncated: %s" % ("true" if pack_truncated else "false")
    if omitted:
        truncation += " (%d file(s) omitted by max-files)" % omitted
    output = "\n\n".join([summary, truncation] + sections)
    _record_direct_tool(
        "context_pack", args, ok=(errors == 0), started=started,
        summary="%d file(s), %d body byte(s), %d error(s)" % (
            len(selected), emitted, errors,
        ),
        output=output,
    )
    activity_tracker.record_event(
        "context_pack",
        summary="%d file(s), %d body byte(s)%s" % (
            len(selected), emitted, " truncated" if pack_truncated else "",
        ),
    )
    return output


@mcp.tool()
def archive_create(
    root: str,
    inputs_json: str,
    destination: str,
    archive_format: str = "zip",
    deterministic: bool = True,
    max_files: int = archive_create_tool.DEFAULT_MAX_FILES,
    max_entries: int = archive_create_tool.DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = archive_create_tool.DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = archive_create_tool.DEFAULT_MAX_TOTAL_BYTES,
    max_depth: int = archive_create_tool.DEFAULT_MAX_DEPTH,
    max_results: int = archive_create_tool.DEFAULT_MAX_RESULTS,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Create a guarded transactional ZIP/TAR from explicit workspace inputs."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "root": root, "inputs_json": inputs_json, "destination": destination,
        "archive_format": archive_format, "deterministic": deterministic,
    }
    try:
        trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
        data = archive_create_tool.create_archive(
            root, inputs_json, destination,
            archive_format=archive_format, deterministic=deterministic is True,
            max_files=max_files, max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes, max_depth=max_depth,
            max_results=max_results, extra_roots=trusted_roots,
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as exc:
        _record_direct_tool(
            "archive_create", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = archive_create_tool.format_result(data)
    _record_direct_tool(
        "archive_create", args, ok=True, started=started,
        summary="%d file(s), %d input bytes" % (data["files"], data["input_bytes"]),
        output=output,
    )
    activity_tracker.record_file_change(
        "create", data["destination"], bytes_written=data["archive_bytes"],
        summary="archive_create: %s, %d entries" % (
            data["archive_format"], data["files"] + data["directories"],
        ),
    )
    return output


@mcp.tool()
def data_convert(
    input_path: str,
    output_path: str,
    fields_json: str,
    output_format: str = "",
    apply: bool = False,
    max_input_bytes: int = 16000000,
    max_output_bytes: int = 16000000,
    max_rows: int = 10000,
    max_columns: int = 100,
    max_fields: int = 50,
    max_field_bytes: int = 64000,
    max_depth: int = 16,
    preview_rows: int = 5,
    timeout: float = 10.0,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Preview or atomically create a deterministic structured-data conversion."""
    _maybe_live_reload()
    started = time.time()
    apply = apply is True
    args = {
        "input_path": input_path, "output_path": output_path,
        "output_format": output_format, "apply": apply,
        "max_input_bytes": max_input_bytes, "max_output_bytes": max_output_bytes,
        "max_rows": max_rows, "max_columns": max_columns,
        "max_fields": max_fields, "max_field_bytes": max_field_bytes,
        "max_depth": max_depth, "preview_rows": preview_rows,
        "timeout": timeout,
    }
    try:
        trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
        report = data_convert_module.convert_data(
            input_path, output_path, fields_json,
            output_format=output_format, apply=apply,
            max_input_bytes=max_input_bytes, max_output_bytes=max_output_bytes,
            max_rows=max_rows, max_columns=max_columns, max_fields=max_fields,
            max_field_bytes=max_field_bytes, max_depth=max_depth,
            preview_rows=preview_rows, timeout=timeout,
            extra_roots=trusted_roots,
        )
    except Exception as exc:
        _record_direct_tool(
            "data_convert", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = data_convert_module.encode_result(report)
    summary = "%s %d row(s), %d byte(s)" % (
        report["mode"], report["rows"], report["converted_bytes"],
    )
    _record_direct_tool(
        "data_convert", args, ok=True, started=started, summary=summary,
        output=output,
    )
    activity_tracker.record_event(
        "data_convert", summary=summary, path=report["output_path"],
    )
    if report["applied"]:
        _record_file_activity("create", {
            "action": "create", "path": report["output_path"],
            "bytes": report["converted_bytes"],
        })
    return output


def _run_read_only_inspection(
    name: str, arguments: dict, *, token: str = "", approval: str = "",
    extra_roots: str = "",
) -> str:
    """Bridge one MCP inspection call through the typed application facade."""
    from sonder_runtime.application.context import local_owner_context

    started = time.time()
    developer = _file_developer_allowed(token)
    authorized = _file_bypass_allowed(token, approval)
    roots = tuple(
        Path(item.strip()).expanduser()
        for item in (extra_roots or "").split(os.pathsep)
        if item.strip()
    ) if authorized else ()
    context = local_owner_context(
        correlation_id="inspection-%s" % os.urandom(4).hex(),
        source="mcp",
        auth_level="developer" if developer else "user" if authorized else "local",
        workspace_roots=roots,
    )
    call_args = dict(arguments)
    call_args["extra_roots"] = extra_roots
    result = _application().inspections.inspect(name, call_args, context)
    evidence = result.evidence or {}
    _record_direct_tool(
        name,
        evidence.get("audit_args", arguments),
        ok=result.ok,
        started=started,
        summary=evidence.get("summary", result.output),
        output=(
            result.output
            if not result.error_code and evidence.get("record_output", True)
            else ""
        ),
    )
    if result.error_code:
        return "ERROR: %s" % result.output
    activity = evidence.get("activity") or {}
    if activity:
        activity_tracker.record_event(
            name,
            summary=activity.get("summary", ""),
            path=activity.get("path", ""),
        )
    return result.output


@mcp.tool()
def log_inspect(
    path: str,
    tail_lines: int = 0,
    context_lines: int = 2,
    max_file_bytes: int = 64000000,
    max_scan_bytes: int = 4000000,
    max_lines: int = 10000,
    max_line_bytes: int = 4096,
    max_results: int = 100,
    max_output_bytes: int = 256000,
    timeout: float = 5.0,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Inspect one guarded text log without execution or caller expressions."""
    _maybe_live_reload()
    args = {
        "path": path, "tail_lines": tail_lines, "context_lines": context_lines,
        "max_file_bytes": max_file_bytes, "max_scan_bytes": max_scan_bytes,
        "max_lines": max_lines, "max_line_bytes": max_line_bytes,
        "max_results": max_results, "max_output_bytes": max_output_bytes,
        "timeout": timeout,
    }
    return _run_read_only_inspection(
        "log_inspect", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def workspace_compare(
    left: str,
    right: str,
    max_entries: int = 2000,
    max_file_bytes: int = 64000000,
    max_total_bytes: int = 256000000,
    max_details: int = 1000,
    max_output_bytes: int = 256000,
    timeout: float = 5.0,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Compare two guarded files/directories by path, type, size, and SHA-256."""
    _maybe_live_reload()
    args = {
        "left": left, "right": right, "max_entries": max_entries,
        "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes,
        "max_details": max_details, "max_output_bytes": max_output_bytes,
        "timeout": timeout,
    }
    return _run_read_only_inspection(
        "workspace_compare", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def project_detect(
    path: str = ".",
    max_depth: int = 8,
    max_files: int = 200,
    max_total_bytes: int = 2_000_000,
    max_file_bytes: int = 256_000,
    max_results: int = 500,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Inventory guarded project manifests and evidence-backed command candidates."""
    _maybe_live_reload()
    args = {
        "path": path, "max_depth": max_depth, "max_files": max_files,
        "max_total_bytes": max_total_bytes, "max_file_bytes": max_file_bytes,
        "max_results": max_results,
    }
    return _run_read_only_inspection(
        "project_detect", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def file_digest(path: str, max_bytes: int = 32_000_000, token: str = "",
                approval: str = "", extra_roots: str = "") -> str:
    """Stream a guarded regular file into a fixed SHA-256 content digest."""
    _maybe_live_reload()
    args = {"path": path, "max_bytes": max_bytes}
    return _run_read_only_inspection(
        "file_digest", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def directory_digest(
    path: str = ".", max_depth: int = 12, max_files: int = 2_000,
    max_total_bytes: int = 32_000_000, max_file_bytes: int = 32_000_000,
    max_results: int = 2_500, token: str = "", approval: str = "",
    extra_roots: str = "",
) -> str:
    """Build a guarded deterministic SHA-256 file manifest and tree Merkle."""
    _maybe_live_reload()
    args = {
        "path": path, "max_depth": max_depth, "max_files": max_files,
        "max_total_bytes": max_total_bytes, "max_file_bytes": max_file_bytes,
        "max_results": max_results,
    }
    return _run_read_only_inspection(
        "directory_digest", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def archive_list(
    path: str,
    max_entries: int = archive_tools.DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = archive_tools.DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = archive_tools.DEFAULT_MAX_TOTAL_BYTES,
    max_ratio: float = archive_tools.DEFAULT_MAX_RATIO,
    max_path_depth: int = archive_tools.DEFAULT_MAX_PATH_DEPTH,
    max_results: int = archive_tools.DEFAULT_MAX_RESULTS,
    max_seconds: float = archive_tools.DEFAULT_MAX_SECONDS,
    token: str = "", approval: str = "", extra_roots: str = "",
) -> str:
    """Prevalidate and list a bounded ZIP/TAR without extracting it."""
    _maybe_live_reload()
    args = {
        "path": path, "max_entries": max_entries,
        "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes,
        "max_ratio": max_ratio, "max_path_depth": max_path_depth,
        "max_results": max_results, "max_seconds": max_seconds,
    }
    return _run_read_only_inspection(
        "archive_list", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def archive_extract(
    source: str, destination: str,
    max_entries: int = archive_tools.DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = archive_tools.DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = archive_tools.DEFAULT_MAX_TOTAL_BYTES,
    max_ratio: float = archive_tools.DEFAULT_MAX_RATIO,
    max_path_depth: int = archive_tools.DEFAULT_MAX_PATH_DEPTH,
    max_seconds: float = archive_tools.DEFAULT_MAX_SECONDS,
    token: str = "", approval: str = "", extra_roots: str = "",
) -> str:
    """Transactionally extract a prevalidated ZIP/TAR to a new directory."""
    _maybe_live_reload()
    started = time.time()
    args = {"source": source, "destination": destination, "max_entries": max_entries}
    try:
        if extra_roots and not _file_bypass_allowed(token, approval):
            raise PermissionError("extra_roots requires developer authorization or approval")
        data = archive_tools.extract_archive(
            source, destination, max_entries=max_entries,
            max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
            max_ratio=max_ratio, max_path_depth=max_path_depth,
            max_seconds=max_seconds, extra_roots=extra_roots,
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as exc:
        _record_direct_tool("archive_extract", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = archive_tools.format_result(data)
    _record_direct_tool("archive_extract", args, ok=True, started=started,
                        summary="%d entries, %d bytes" % (data["entries"], data["bytes"]), output=output)
    activity_tracker.record_file_change(
        "create_directory", data["destination"], bytes_written=data["bytes"],
        summary="archive_extract: %d entries" % data["entries"],
    )
    return output


@mcp.tool()
def data_inspect(
    path: str,
    max_bytes: int = 256000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Structured, read-only preview of a data file inside allowed roots.

    Understands JSON, JSONL/NDJSON, TOML, YAML, CSV, TSV, SQLite databases,
    ZIP/TAR archives, and INI/CFG by suffix; unknown types fall back to
    text statistics or a binary signature. Never executes file contents and
    returns only a bounded preview (keys, columns, table row counts,
    archive members, sample rows).
    """
    _maybe_live_reload()
    return _run_read_only_inspection(
        "data_inspect", {"path": path, "max_bytes": max_bytes},
        token=token, approval=approval, extra_roots=extra_roots,
    )


@mcp.tool()
def data_query(
    path: str,
    sql: str = "",
    projection_json: str = "[]",
    filters_json: str = "{}",
    max_rows: int = 100,
    max_columns: int = 50,
    max_output_bytes: int = 256000,
    max_scan_bytes: int = 4000000,
    timeout: float = 5.0,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Run a bounded read-only SQLite or structured text data query."""
    _maybe_live_reload()
    args = {
        "path": path, "sql": sql, "projection_json": projection_json,
        "filters_json": filters_json, "max_rows": max_rows,
        "max_columns": max_columns, "max_output_bytes": max_output_bytes,
        "max_scan_bytes": max_scan_bytes, "timeout": timeout,
    }
    return _run_read_only_inspection(
        "data_query", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def sqlite_mutate(
    path: str,
    sql: str,
    parameters_json: str,
    mode: str = "preview",
    max_rows: int = 1000,
    timeout: float = 2.0,
    max_db_bytes: int = 67108864,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Preview-rollback or atomically apply one parameterized SQLite DML statement."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "statement_chars": len(sql) if isinstance(sql, str) else 0,
        "parameters_chars": len(parameters_json) if isinstance(parameters_json, str) else 0,
        "mode": mode, "max_rows": max_rows, "timeout": timeout,
        "max_db_bytes": max_db_bytes,
    }
    try:
        data = sqlite_mutate_module.mutate_sqlite(
            path, sql, parameters_json, mode=mode, max_rows=max_rows,
            timeout=timeout, max_db_bytes=max_db_bytes, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "sqlite_mutate", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    _record_direct_tool(
        "sqlite_mutate", args, ok=True, started=started,
        summary="%s %s %d row(s)" % (
            data["mode"], data["statement"], data["rows_affected"],
        ), output=output,
    )
    if data["applied"]:
        _record_file_activity("sqlite_mutate", {
            "action": "sqlite_mutate", "path": data["path"],
            "bytes": data["database_bytes_after"],
            "lines_edited": data["rows_affected"],
        })
    return output


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
    _record_file_activity("write", data, preview=content, preview_kind="content")
    return _format_file_result("file write", data)


@mcp.tool()
def file_copy(
    source: str,
    destination: str,
    overwrite: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Copy one bounded binary-safe file inside allowed roots."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "source": source, "destination": destination,
        "overwrite": overwrite,
    }
    try:
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        data = file_ops.copy_file(
            source,
            destination,
            overwrite=overwrite,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as exc:
        _record_direct_tool(
            "file_copy", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    _record_direct_tool(
        "file_copy", args, ok=True, started=started,
        summary="%s bytes" % data.get("bytes", 0),
    )
    _record_file_activity("copy", data)
    return _format_file_result("file copy", data)


@mcp.tool()
def file_move(
    source: str,
    destination: str,
    overwrite: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Move one bounded binary-safe file inside allowed roots."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "source": source, "destination": destination,
        "overwrite": overwrite,
    }
    try:
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        data = file_ops.move_file(
            source,
            destination,
            overwrite=overwrite,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as exc:
        _record_direct_tool(
            "file_move", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    _record_direct_tool(
        "file_move", args, ok=True, started=started,
        summary="%s bytes" % data.get("bytes", 0),
    )
    activity_tracker.record_file_change(
        "move_source", data.get("source", ""),
        summary="moved to %s" % data.get("destination", ""),
    )
    _record_file_activity("move", data)
    return _format_file_result("file move", data)


@mcp.tool()
def file_batch_write(
    operations_json: str,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Transactionally create/overwrite a bounded JSON list of project files."""
    _maybe_live_reload()
    started = time.time()
    args = {"input_chars": len(operations_json) if isinstance(operations_json, str) else 0}
    try:
        data = file_ops.batch_write_files(
            operations_json,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except file_ops.BatchWriteError as exc:
        output = json.dumps(exc.report, indent=2, sort_keys=True)
        _record_direct_tool(
            "file_batch_write", args, ok=False, started=started,
            summary=str(exc), output=output,
        )
        return "ERROR: %s" % output
    except Exception as exc:
        _record_direct_tool(
            "file_batch_write", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool(
        "file_batch_write", args, ok=True, started=started,
        summary="%d file(s) committed" % data["count"], output=output,
    )
    for result in data["results"]:
        for created_directory in result.get("created_directories", []):
            activity_tracker.record_file_change(
                "create_directory", created_directory,
                summary="parent created by file_batch_write",
            )
        _record_file_activity(result.get("action", "write"), result)
    return output


@mcp.tool()
def json_patch(
    path: str,
    operations_json: str,
    mode: str = "preview",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Preview or atomically apply a bounded RFC 6902 subset to one JSON file."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "mode": mode,
        "input_chars": len(operations_json) if isinstance(operations_json, str) else 0,
    }
    try:
        data = json_patch_tool.patch_json(
            path, operations_json, mode=mode, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except json_patch_tool.JsonPatchError as exc:
        output = json.dumps(exc.report, indent=2, sort_keys=True, ensure_ascii=False)
        _record_direct_tool(
            "json_patch", args, ok=False, started=started,
            summary=str(exc), output=output,
        )
        return "ERROR: %s" % output
    except Exception as exc:
        _record_direct_tool(
            "json_patch", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    _record_direct_tool(
        "json_patch", args, ok=True, started=started,
        summary="%s %d operation(s)" % (data["mode"], data["operations"]),
        output=output,
    )
    if data["applied"]:
        _record_file_activity("json_patch", {
            "action": "json_patch", "path": data["path"],
            "bytes": data["bytes_after"], "lines_edited": data["operations"],
        })
    return output


@mcp.tool()
def text_patch(
    root: str,
    patch: str,
    apply: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Strictly preview or transactionally apply a bounded unified text diff."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "patch_bytes": len(patch.encode("utf-8")) if isinstance(patch, str) else 0,
            "apply": bool(apply)}
    try:
        trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
        data = text_patch_ops.text_patch(
            root, patch, apply=bool(apply), extra_roots=trusted_roots,
            developer_authorized=_file_developer_allowed(token),
        )
    except text_patch_ops.TextPatchError as exc:
        output = json.dumps(exc.report, indent=2, sort_keys=True)
        _record_direct_tool("text_patch", args, ok=False, started=started, summary=str(exc), output=output)
        return "ERROR: %s" % output
    except Exception as exc:
        _record_direct_tool("text_patch", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool("text_patch", args, ok=True, started=started,
                        summary="%s %d file(s)" % ("applied" if apply else "previewed", len(data["files"])),
                        output=output)
    if apply:
        for row in data["files"]:
            activity_tracker.record_file_change(
                "create" if row["action"] == "create" else "edit",
                str(Path(data["root"]) / Path(*row["path"].split("/"))),
                summary="text_patch %s" % row["action"],
                preview=patch,
                preview_kind="diff",
            )
    return output


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
    diff_preview = "--- selected text\n+++ replacement text\n- %s\n+ %s" % (old, new)
    _record_file_activity(
        "edit", data, preview=diff_preview, preview_kind="diff",
    )
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
    recursive = recursive is True
    dry_run = dry_run is not False
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
def repo_status(
    root: str = ".",
    timeout: int = 10,
    max_output: int = 128000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Inspect bounded read-only Git branch and worktree status at a repo root."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "timeout": timeout, "max_output": max_output}
    try:
        data = git_tools.repo_status(
            root,
            timeout=timeout,
            max_output=max_output,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "repo_status", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    lines = [
        "repository status: %s" % data["root"],
        "  branch: %s" % (
            "(detached at %s)" % data["oid"][:12]
            if data["detached"] else (data["branch"] or "(unborn)")
        ),
        "  upstream: %s | ahead: %d | behind: %d" % (
            data["upstream"] or "(none)", data["ahead"], data["behind"],
        ),
        "  clean: %s | changes: %d | elapsed_ms: %d" % (
            "unknown" if data["clean"] is None else str(data["clean"]).lower(),
            data["change_count"], data["elapsed_ms"],
        ),
    ]
    lines.extend("  %s" % entry for entry in data["entries"])
    if data["truncated"]:
        lines.append(
            "  ... output truncated at %d bytes; status may be incomplete"
            % data["output_limit"]
        )
    output = "\n".join(lines)
    _record_direct_tool(
        "repo_status", args, ok=True, started=started,
        summary="%d change(s)" % data["change_count"], output=output,
    )
    return output


@mcp.tool()
def repo_diff(
    root: str = ".",
    staged: bool = False,
    path: str = "",
    context: int = 3,
    timeout: int = 10,
    max_output: int = 128000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Inspect a bounded read-only unstaged or staged Git diff at a repo root."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "root": root, "staged": staged, "path": path, "context": context,
        "timeout": timeout, "max_output": max_output,
    }
    try:
        data = git_tools.repo_diff(
            root,
            staged=staged is True,
            path=path,
            context=context,
            timeout=timeout,
            max_output=max_output,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "repo_diff", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    scope = data["path"] or "(all tracked paths)"
    lines = [
        "repository diff: %s" % data["root"],
        "  mode: %s | path: %s | context: %d | elapsed_ms: %d" % (
            "staged" if data["staged"] else "unstaged",
            scope, data["context"], data["elapsed_ms"],
        ),
    ]
    if data["diff"]:
        lines.append(data["diff"].rstrip())
    else:
        lines.append("  (no diff)")
    if data["truncated"]:
        lines.append(
            "  ... output truncated at %d bytes" % data["output_limit"]
        )
    output = "\n".join(lines)
    _record_direct_tool(
        "repo_diff", args, ok=True, started=started,
        summary="%s diff" % ("staged" if data["staged"] else "unstaged"),
        output=output,
    )
    return output


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
    """Summarize a guarded workspace; ignored paths require developer authentication."""
    _maybe_live_reload()
    started = time.time()
    policy_error = _include_ignored_error(
        "workspace_inventory", include_ignored, token,
    )
    if policy_error:
        return policy_error
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
def dependency_inventory(
    path: str = ".",
    max_depth: int = 5,
    max_files: int = 100,
    max_total_bytes: int = 2000000,
    max_results: int = 2000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Parse bounded dependency manifests and lockfiles without execution/network."""
    _maybe_live_reload()
    args = {
        "path": path, "max_depth": max_depth, "max_files": max_files,
        "max_total_bytes": max_total_bytes, "max_results": max_results,
    }
    return _run_read_only_inspection(
        "dependency_inventory", args, token=token, approval=approval,
        extra_roots=extra_roots,
    )


@mcp.tool()
def directory_tree(
    path: str = ".",
    depth: int = 2,
    max_entries: int = 200,
    include_hidden: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
    include_ignored: bool = False,
) -> str:
    """List a bounded guarded tree; ignored paths require developer authentication."""
    _maybe_live_reload()
    started = time.time()
    policy_error = _include_ignored_error("directory_tree", include_ignored, token)
    if policy_error:
        return policy_error
    args = {"path": path, "depth": depth, "max_entries": max_entries}
    try:
        data = workbench.directory_tree(
            path, depth=depth, max_entries=max_entries,
            include_hidden=include_hidden, include_ignored=include_ignored,
            extra_roots=extra_roots,
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
        # read_line_range lives in workbench; apply the same secret/control-plane
        # read guard the in-module read tools now enforce, before touching it.
        file_ops.require_read_access(
            path, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
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
    """Search guarded files; ignored paths require developer authentication.

    `query` is matched LITERALLY against file contents -- a substring, or a
    regular expression when regex=True. It is NOT a description of what you are
    looking for. Search for the identifier or the exact code you expect to find
    ("normalize_rules", "class VerifierUnavailable"), never for a phrase about
    it ("normalize_rules implementation", "where rules are validated"): a phrase
    that does not appear verbatim in any file matches nothing.

    An empty result is an answer. It means that text is not present, so the next
    step is a DIFFERENT pattern -- a shorter one, a different identifier, or
    file_find/file_read_range to look directly. Repeating a query that returned
    nothing returns nothing again; two self-modification runs died that way, each
    reissuing one malformed natural-language query three times until the
    repetition guard stopped them, after an earlier search had already returned
    the evidence they needed.
    """
    _maybe_live_reload()
    started = time.time()
    policy_error = _include_ignored_error("text_search", include_ignored, token)
    if policy_error:
        return policy_error
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
    """Find guarded scripts; ignored paths require developer authentication."""
    _maybe_live_reload()
    started = time.time()
    policy_error = _include_ignored_error("script_search", include_ignored, token)
    if policy_error:
        return policy_error
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
    if data.get("truncated"):
        # workbench stops scanning PATH at its candidate cap and cuts at
        # max_results BEFORE sorting, so the alphabetised rows below are a
        # PATH-order slice, not the machine's program list. Dropping this flag
        # made "not in the list" read as "not installed" -- every sibling
        # search handler surfaces it.
        lines.append(
            "  ... truncated: cut at %d result(s) -- more may match; narrow "
            "the query or raise max_results" % len(data["results"])
        )
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


# ── Sonder developer-workflow tools ──────────────────────────────────────


@mcp.tool()
def test_discover(
    root: str = ".",
    framework: str = "auto",
) -> str:
    """Discover tests in a project — detects the test framework (pytest, jest, vitest, cargo, go, dotnet) and lists test files and counts."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "framework": framework}
    try:
        data = harness_tools.test_discover(root=root, framework=framework)
    except Exception as exc:
        _record_direct_tool("test_discover", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    _record_direct_tool(
        "test_discover", args, ok=True, started=started,
        summary="%s: %d tests in %d files" % (data.get("framework"), data.get("test_count", 0), len(data.get("test_files", []))),
    )
    lines = ["test discovery: %s" % data.get("framework"), "  tests: %d" % data.get("test_count", 0)]
    if data.get("test_files"):
        lines.append("  files:")
        for f in data["test_files"][:50]:
            lines.append("    %s" % f)
    if data.get("error"):
        lines.append("  error: %s" % data["error"])
    return "\n".join(lines)


@mcp.tool()
def test_run(
    root: str = ".",
    framework: str = "auto",
    path: str = "",
    pattern: str = "",
    verbose: bool = False,
    coverage: bool = False,
    timeout: int = 120,
    extra_args_json: str = "[]",
) -> str:
    """Run tests with auto-detected or specified framework (pytest, jest, vitest, cargo, go, mocha, dotnet). Supports filtering by path/pattern, coverage, and extra args."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "framework": framework, "path": path, "pattern": pattern, "timeout": timeout}
    try:
        data = harness_tools.test_run(
            root=root, framework=framework, path=path, pattern=pattern,
            verbose=verbose, coverage=coverage, timeout=timeout,
            extra_args_json=extra_args_json,
        )
    except Exception as exc:
        _record_direct_tool("test_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("test run (%s)" % data.get("framework", "?"), data)
    _record_direct_tool(
        "test_run", args, ok=data.get("ok", False), started=started,
        summary="exit %s" % data.get("returncode"),
        output=output,
    )
    return output


@mcp.tool()
def lint_run(
    root: str = ".",
    tool: str = "auto",
    path: str = "",
    fix: bool = False,
    timeout: int = 60,
) -> str:
    """Run a linter (ruff, flake8, pylint, eslint, clippy) with auto-detection. Set fix=true to auto-fix."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "tool": tool, "path": path, "fix": fix, "timeout": timeout}
    try:
        data = harness_tools.lint_run(root=root, tool=tool, path=path, fix=fix, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("lint_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("lint (%s, %s)" % (data.get("tool", "?"), data.get("mode", "check")), data)
    _record_direct_tool(
        "lint_run", args, ok=data.get("ok", False), started=started,
        summary="exit %s" % data.get("returncode"),
        output=output,
    )
    return output


@mcp.tool()
def format_code(
    root: str = ".",
    tool: str = "auto",
    path: str = "",
    check_only: bool = False,
    timeout: int = 60,
) -> str:
    """Format code (ruff, black, prettier, rustfmt, gofmt, clang-format) with auto-detection. Set check_only=true to verify without writing."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "tool": tool, "path": path, "check_only": check_only, "timeout": timeout}
    try:
        data = harness_tools.format_code(root=root, tool=tool, path=path, check_only=check_only, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("format_code", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("format (%s, %s)" % (data.get("tool", "?"), data.get("mode", "format")), data)
    _record_direct_tool(
        "format_code", args, ok=data.get("ok", False), started=started,
        summary="exit %s" % data.get("returncode"),
        output=output,
    )
    return output


@mcp.tool()
def typecheck_run(
    root: str = ".",
    tool: str = "auto",
    path: str = "",
    timeout: int = 120,
) -> str:
    """Run a type checker (mypy, pyright, tsc) with auto-detection."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "tool": tool, "path": path, "timeout": timeout}
    try:
        data = harness_tools.typecheck_run(root=root, tool=tool, path=path, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("typecheck_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("typecheck (%s)" % data.get("tool", "?"), data)
    _record_direct_tool(
        "typecheck_run", args, ok=data.get("ok", False), started=started,
        summary="exit %s" % data.get("returncode"),
        output=output,
    )
    return output


@mcp.tool()
def dependency_add(
    root: str = ".",
    packages_json: str = "[]",
    dev: bool = False,
    timeout: int = 60,
) -> str:
    """Install packages (pip, npm, pnpm, yarn, cargo, go) with auto-detected package manager. Pass packages as a JSON array."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "packages_json": packages_json, "dev": dev, "timeout": timeout}
    try:
        data = harness_tools.dependency_add(root=root, packages_json=packages_json, dev=dev, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("dependency_add", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("dependency add (%s)" % data.get("manager", "?"), data)
    _record_direct_tool(
        "dependency_add", args, ok=data.get("ok", False), started=started,
        summary="%s: %s" % (data.get("manager"), data.get("packages", [])),
        output=output,
    )
    return output


@mcp.tool()
def dependency_remove(
    root: str = ".",
    packages_json: str = "[]",
    timeout: int = 60,
) -> str:
    """Uninstall packages with auto-detected package manager. Pass packages as a JSON array."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "packages_json": packages_json, "timeout": timeout}
    try:
        data = harness_tools.dependency_remove(root=root, packages_json=packages_json, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("dependency_remove", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("dependency remove (%s)" % data.get("manager", "?"), data)
    _record_direct_tool(
        "dependency_remove", args, ok=data.get("ok", False), started=started,
        summary="%s: %s" % (data.get("manager"), data.get("packages", [])),
        output=output,
    )
    return output


@mcp.tool()
def dependency_update(
    root: str = ".",
    packages_json: str = "[]",
    timeout: int = 120,
) -> str:
    """Update packages (or all if empty array) with auto-detected package manager."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "packages_json": packages_json, "timeout": timeout}
    try:
        data = harness_tools.dependency_update(root=root, packages_json=packages_json, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("dependency_update", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("dependency update (%s)" % data.get("manager", "?"), data)
    _record_direct_tool(
        "dependency_update", args, ok=data.get("ok", False), started=started,
        summary=data.get("manager"),
        output=output,
    )
    return output


@mcp.tool()
def dependency_audit(
    root: str = ".",
    timeout: int = 60,
) -> str:
    """Audit installed dependencies for known vulnerabilities (pip check, npm audit, cargo audit)."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "timeout": timeout}
    try:
        data = harness_tools.dependency_audit(root=root, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("dependency_audit", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    if "command" in data:
        output = _format_run_result("dependency audit (%s)" % data.get("manager", "?"), data)
    else:
        output = json.dumps(data, ensure_ascii=False)
    _record_direct_tool(
        "dependency_audit", args, ok=data.get("ok", False), started=started,
        summary=data.get("manager"),
        output=output,
    )
    return output


@mcp.tool()
def git_commit(
    root: str = ".",
    message: str = "",
    paths_json: str = "[]",
    all_tracked: bool = False,
    timeout: int = 30,
) -> str:
    """Create a git commit. Stage specific files via paths_json (JSON array) or set all_tracked=true for all modified tracked files. Never stages untracked files by default."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "message": message, "paths_json": paths_json, "all_tracked": all_tracked}
    try:
        data = harness_tools.git_commit(root=root, message=message, paths_json=paths_json, all_tracked=all_tracked, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_commit", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git commit", data)
    _record_direct_tool(
        "git_commit", args, ok=data.get("ok", False), started=started,
        summary="exit %s" % data.get("returncode"),
        output=output,
    )
    return output


@mcp.tool()
def git_branch(
    root: str = ".",
    name: str = "",
    checkout: bool = True,
    base: str = "",
    timeout: int = 10,
) -> str:
    """Create a git branch, optionally checking it out and basing it on a ref."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "name": name, "checkout": checkout, "base": base}
    try:
        data = harness_tools.git_branch(root=root, name=name, checkout=checkout, base=base, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_branch", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git branch", data)
    _record_direct_tool("git_branch", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def git_checkout(
    root: str = ".",
    ref: str = "",
    timeout: int = 10,
) -> str:
    """Switch to a branch, tag, or commit."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "ref": ref}
    try:
        data = harness_tools.git_checkout(root=root, ref=ref, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_checkout", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git checkout", data)
    _record_direct_tool("git_checkout", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def git_stash(
    root: str = ".",
    action: str = "push",
    message: str = "",
    include_untracked: bool = True,
    timeout: int = 10,
) -> str:
    """Manage the git stash: push (save changes), pop (restore), list, or drop."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "action": action, "message": message}
    try:
        data = harness_tools.git_stash(root=root, action=action, message=message, include_untracked=include_untracked, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_stash", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git stash %s" % action, data)
    _record_direct_tool("git_stash", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def git_tag(
    root: str = ".",
    name: str = "",
    message: str = "",
    delete: bool = False,
    timeout: int = 10,
) -> str:
    """Create or delete a git tag. Set message for an annotated tag."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "name": name, "message": message, "delete": delete}
    try:
        data = harness_tools.git_tag(root=root, name=name, message=message, delete=delete, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_tag", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git tag", data)
    _record_direct_tool("git_tag", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def git_merge(
    root: str = ".",
    branch: str = "",
    no_ff: bool = True,
    message: str = "",
    timeout: int = 30,
) -> str:
    """Merge a branch into the current branch."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "branch": branch, "no_ff": no_ff, "message": message}
    try:
        data = harness_tools.git_merge(root=root, branch=branch, no_ff=no_ff, message=message, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_merge", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git merge", data)
    _record_direct_tool("git_merge", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def git_cherry_pick(
    root: str = ".",
    commits_json: str = "[]",
    timeout: int = 30,
) -> str:
    """Cherry-pick one or more commits onto the current branch. Pass commit SHAs as a JSON array."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "commits_json": commits_json}
    try:
        data = harness_tools.git_cherry_pick(root=root, commits_json=commits_json, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("git_cherry_pick", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("git cherry-pick", data)
    _record_direct_tool("git_cherry_pick", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def build_run(
    root: str = ".",
    command: str = "",
    timeout: int = 120,
) -> str:
    """Run the project build system (auto-detects Make, Cargo, CMake, Go, npm, Gradle, Maven). Pass a custom command to override auto-detection."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "command": command, "timeout": timeout}
    try:
        data = harness_tools.build_run(root=root, command=command, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("build_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("build", data)
    _record_direct_tool("build_run", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def build_clean(
    root: str = ".",
    timeout: int = 30,
) -> str:
    """Clean build artifacts (auto-detects Make, Cargo, Go)."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "timeout": timeout}
    try:
        data = harness_tools.build_clean(root=root, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("build_clean", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("build clean", data)
    _record_direct_tool("build_clean", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def rename_symbol(
    root: str = ".",
    old_name: str = "",
    new_name: str = "",
    glob: str = "**/*.py",
    dry_run: bool = True,
) -> str:
    """Rename a symbol across files matching a glob pattern. Returns a preview by default; set dry_run=false to apply."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "old_name": old_name, "new_name": new_name, "glob": glob, "dry_run": dry_run}
    try:
        data = harness_tools.rename_symbol(root=root, old_name=old_name, new_name=new_name, glob=glob, dry_run=dry_run)
    except Exception as exc:
        _record_direct_tool("rename_symbol", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = [
        "rename: %s → %s (%s)" % (old_name, new_name, "dry run" if dry_run else "applied"),
        "  files: %d" % data.get("files_changed", 0),
        "  replacements: %d" % data.get("total_replacements", 0),
    ]
    if data.get("preview"):
        lines.append("  preview:")
        for p in data["preview"]:
            lines.append("    %s:%d  %s" % (p["file"], p["line"], p["text"]))
    output = "\n".join(lines)
    _record_direct_tool(
        "rename_symbol", args, ok=data.get("ok", False), started=started,
        summary="%d replacements in %d files" % (data.get("total_replacements", 0), data.get("files_changed", 0)),
        output=output,
    )
    return output


@mcp.tool()
def find_references(
    root: str = ".",
    symbol: str = "",
    glob: str = "**/*.py",
) -> str:
    """Find all references to a symbol across files matching a glob. Returns file, line number, and context for each reference."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "symbol": symbol, "glob": glob}
    try:
        data = harness_tools.extract_references(root=root, symbol=symbol, glob=glob)
    except Exception as exc:
        _record_direct_tool("find_references", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = ["references to '%s'" % symbol]
    refs = data.get("references", [])
    for r in refs:
        lines.append("  %s:%d  %s" % (r["file"], r["line"], r["text"]))
    if data.get("truncated"):
        lines.append("  ... (truncated at 200 results)")
    lines.insert(1, "  total: %d%s" % (len(refs), " (truncated)" if data.get("truncated") else ""))
    output = "\n".join(lines)
    _record_direct_tool(
        "find_references", args, ok=data.get("ok", False), started=started,
        summary="%d references" % len(refs),
        output=output,
    )
    return output


@mcp.tool()
def diff_files(
    root: str = ".",
    left: str = "",
    right: str = "",
    context: int = 3,
) -> str:
    """Compute a unified diff between two files in the workspace."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "left": left, "right": right, "context": context}
    try:
        data = harness_tools.diff_files(root=root, left=left, right=right, context=context)
    except Exception as exc:
        _record_direct_tool("diff_files", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = data.get("stdout", "") or data.get("stderr", "") or "(files are identical)"
    _record_direct_tool("diff_files", args, ok=data.get("ok", False), started=started, summary="diff %s %s" % (left, right), output=output)
    return output


@mcp.tool()
def apply_patch(
    root: str = ".",
    patch_text: str = "",
    check_only: bool = False,
) -> str:
    """Apply a unified diff patch to the workspace. Set check_only=true to verify without writing."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "check_only": check_only}
    try:
        data = harness_tools.apply_patch(root=root, patch_text=patch_text, check_only=check_only)
    except Exception as exc:
        _record_direct_tool("apply_patch", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("apply patch%s" % (" (check)" if check_only else ""), data)
    _record_direct_tool("apply_patch", args, ok=data.get("ok", False), started=started, summary="exit %s" % data.get("returncode"), output=output)
    return output


@mcp.tool()
def secret_scan(
    root: str = ".",
    timeout: int = 30,
) -> str:
    """Scan workspace files for leaked secrets (API keys, passwords, private keys, tokens). Returns findings with file, line, and type."""
    _maybe_live_reload()
    started = time.time()
    args = {"root": root, "timeout": timeout}
    try:
        data = harness_tools.secret_scan(root=root, timeout=timeout)
    except Exception as exc:
        _record_direct_tool("secret_scan", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    findings = data.get("findings", [])
    lines = [
        "secret scan: %d finding(s) in %d files scanned" % (len(findings), data.get("files_scanned", 0)),
    ]
    for f in findings:
        lines.append("  %s:%d  [%s]  %s" % (f["file"], f["line"], f["type"], f["match"]))
    if data.get("truncated"):
        lines.append("  ... (truncated at 100 findings)")
    output = "\n".join(lines)
    _record_direct_tool(
        "secret_scan", args, ok=data.get("ok", False), started=started,
        summary="%d findings" % len(findings),
        output=output,
    )
    return output


@mcp.tool()
def process_list(max_processes: int = 128, max_seconds: float = 0.5) -> str:
    """List bounded process metadata when host inspection is explicitly enabled."""
    _maybe_live_reload()
    started = time.time()
    args = {"max_processes": max_processes, "max_seconds": max_seconds}
    try:
        data = process_risk_module.list_processes(
            max_processes=max_processes, max_seconds=max_seconds,
        )
    except Exception as exc:
        _record_direct_tool("process_list", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    _record_direct_tool(
        "process_list", args, ok=bool(data.get("ok")), started=started,
        summary="%s; %d process(es)" % (data.get("status"), data.get("process_count", 0)),
        output=output,
    )
    return output


@mcp.tool()
def process_memory_risk_inspect(
    pid: int,
    max_bytes: int = 4 * 1024 * 1024,
    max_regions: int = 256,
    max_seconds: float = 1.0,
) -> str:
    """Inspect one PID for fixed memory-risk indicators without returning content."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "pid": pid, "max_bytes": max_bytes, "max_regions": max_regions,
        "max_seconds": max_seconds,
    }
    try:
        data = process_risk_module.inspect_process_memory(
            pid, max_bytes=max_bytes, max_regions=max_regions,
            max_seconds=max_seconds,
        )
    except Exception as exc:
        _record_direct_tool(
            "process_memory_risk_inspect", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    _record_direct_tool(
        "process_memory_risk_inspect", args, ok=bool(data.get("ok")),
        started=started,
        summary="%s; %s risk" % (data.get("status"), data.get("risk", "unknown")),
        output=output,
    )
    return output


@mcp.tool()
def artifact_risk_inspect(
    path: str,
    max_scan_bytes: int = 16 * 1024 * 1024,
    max_seconds: float = 5.0,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Statically inspect a guarded document, executable, script, or binary."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path, "max_scan_bytes": max_scan_bytes, "max_seconds": max_seconds}
    trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
    try:
        data = artifact_risk_module.inspect_artifact(
            path,
            max_scan_bytes=max_scan_bytes,
            max_seconds=max_seconds,
            extra_roots=trusted_roots,
        )
    except Exception as exc:
        _record_direct_tool(
            "artifact_risk_inspect", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = artifact_risk_module.format_result(data)
    _record_direct_tool(
        "artifact_risk_inspect", args, ok=True, started=started,
        summary="%s risk; %s" % (data.get("risk"), data.get("kind", "artifact")),
        output=output,
    )
    activity_tracker.record_event(
        "artifact_risk_inspect",
        summary="%s risk; %s" % (data.get("risk"), data.get("kind", "artifact")),
        path=data.get("path", ""),
    )
    return output


@mcp.tool()
def fetch_artifact(
    url: str,
    dest: str,
    expect_type: str = "",
    expect_publisher: str = "",
    sha256: str = "",
    max_mb: float = artifact_fetch_module.DEFAULT_MAX_MB,
    timeout: float = artifact_fetch_module.DEFAULT_TIMEOUT,
    resume: bool = True,
    overwrite: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Download a binary artifact to a guarded path and verify it atomically.

    Unlike web_fetch this writes bytes, not text, so it is the only supported
    way to acquire an installer, driver, ISO, or archive. Nothing lands at
    *dest* unless the payload passes every check: HTTP status, block/denial
    page detection, magic bytes against expect_type or the extension, a size
    floor, an optional sha256, and -- on Windows for PE payloads -- the
    Authenticode signer subject against expect_publisher. Success also writes
    <dest>.provenance.json recording the URL, redirect chain, digest,
    signature, and every verdict.
    """
    _maybe_live_reload()
    started = time.time()
    args = {
        "url": url, "dest": dest, "expect_type": expect_type,
        "expect_publisher": expect_publisher, "sha256": sha256,
        "max_mb": max_mb, "timeout": timeout,
    }
    try:
        data = artifact_fetch_module.fetch_artifact(
            url,
            dest,
            expect_type=expect_type,
            expect_publisher=expect_publisher,
            sha256=sha256,
            max_mb=max_mb,
            timeout=timeout,
            resume=resume,
            overwrite=overwrite,
            extra_roots=extra_roots if _file_bypass_allowed(token, approval) else "",
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "fetch_artifact", args, ok=False, started=started, summary=str(exc),
        )
        return "artifact fetch REFUSED: %s" % exc
    output = artifact_fetch_module.format_fetch_result(data)
    summary = "%s; %d bytes; %s" % (
        data.get("verdict", "rejected"), data.get("bytes", 0),
        data.get("detected_type", "") or "unknown",
    )
    _record_direct_tool(
        "fetch_artifact", args, ok=bool(data.get("ok")), started=started,
        summary=summary, output=output,
    )
    if data.get("ok"):
        _record_file_activity("write", {
            "action": "fetch_artifact",
            "path": data.get("path", ""),
            "bytes": data.get("bytes", 0),
        })
    activity_tracker.record_event(
        "fetch_artifact", summary=summary, path=data.get("path", ""),
    )
    return output


@mcp.tool()
def verify_artifact(
    path: str,
    expect_type: str = "",
    expect_publisher: str = "",
    sha256: str = "",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Run fetch_artifact's verification battery against a file already on disk.

    Same code path as a fresh download: magic bytes vs expect_type or the
    extension, block/denial-page markers, a per-type size floor, an optional
    sha256, and the Authenticode signer subject against expect_publisher. Use
    it on anything staged earlier or acquired outside Sonder.
    """
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "expect_type": expect_type,
        "expect_publisher": expect_publisher, "sha256": sha256,
    }
    try:
        data = artifact_fetch_module.verify_artifact(
            path,
            expect_type=expect_type,
            expect_publisher=expect_publisher,
            sha256=sha256,
            extra_roots=extra_roots if _file_bypass_allowed(token, approval) else "",
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "verify_artifact", args, ok=False, started=started, summary=str(exc),
        )
        return "artifact verify REFUSED: %s" % exc
    output = artifact_fetch_module.format_verify_result(data)
    summary = "%s; %d bytes; %s" % (
        data.get("verdict", "rejected"), data.get("bytes", 0),
        data.get("detected_type", "") or "unknown",
    )
    _record_direct_tool(
        "verify_artifact", args, ok=bool(data.get("ok")), started=started,
        summary=summary, output=output,
    )
    activity_tracker.record_event(
        "verify_artifact", summary=summary, path=data.get("path", ""),
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
    risk_policy: str = "",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Run a guarded script with its known interpreter and bounded output."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "args_json": args_json, "cwd": cwd, "timeout": timeout,
        "risk_policy": risk_policy,
    }
    try:
        trusted_roots = extra_roots if _file_bypass_allowed(token, approval) else ""
        risk = artifact_risk_module.enforce_execution_policy(
            path, requested=risk_policy, extra_roots=trusted_roots,
        )
        if str(risk.get("policy", "")).startswith("deny-"):
            # A scan followed by a pathname-based interpreter launch is not an
            # exact-file handoff: another same-user process could replace the
            # path between those operations. Until the runner can execute the
            # already-inspected handle cross-platform, enforcing policies fail
            # closed even when the static result itself is below the threshold.
            refused = dict(risk)
            refused.update({
                "denied": True,
                "denial_reason": "exact_execution_handoff_unavailable",
            })
            raise artifact_risk_module.ArtifactRiskDenied(refused)
        data = workbench.run_script(
            path, args_json=args_json, cwd=cwd, stdin=stdin, timeout=timeout,
            max_output=max_output, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except artifact_risk_module.ArtifactRiskDenied as exc:
        output = (
            "artifact risk: %s\nexecution denied by effective policy %s"
            % (
                artifact_risk_module.format_result(exc.result),
                exc.result.get("policy", "unknown"),
            )
        )
        _record_direct_tool(
            "script_run", args, ok=False, started=started,
            summary=str(exc), output=output,
        )
        return output
    except Exception as exc:
        _record_direct_tool("script_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = (
        "artifact risk: %s\n%s\n%s"
        % (
            artifact_risk_module.format_result(risk),
            "execution allowed by effective policy %s" % risk.get("policy", "off"),
            _format_run_result("script run", data),
        )
    )
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
        # image_inspect lives in workbench; apply the same secret/control-plane
        # read guard the in-module read tools now enforce, before touching it.
        file_ops.require_read_access(
            path, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
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
    emb = embeddings.embed(text)
    if not embeddings.valid_vector(emb):
        emb = None
    blob = embeddings.to_blob(emb) if emb else None
    fact_id = memory_store.new_id()
    # SPEC-3: fact persistence routes through the UnitOfWork-owned
    # MemoryRepository; passing _DB_PATH keeps the tool on the server's
    # database (tests repoint it), with the same connection semantics.
    with _application().unit_of_work(db_path=_DB_PATH) as uow:
        uow.memory.add_fact(fact_id, project_id, text, blob)
        n = uow.memory.count_facts(project_id)
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
def isolated_run(
    image: str,
    argv_json: str,
    project: str,
    stdin: str = "",
    writable_workspace: bool = False,
    timeout: int = 30,
    memory_mb: int = 512,
    cpus: float = 1.0,
    pids: int = 64,
    output_bytes: int = 131072,
    acknowledge_isolation_limits: bool = False,
    token: str = "",
    approval: str = "",
    write_approval: str = "",
) -> str:
    """Run an installed Linux container image under a fixed isolation policy.

    This direct MCP tool requires developer authentication, a host approval
    secret, and explicit risk acknowledgement in addition to the local ``ask``
    policy. Writable binds require a second host secret. It is intentionally
    unavailable to Sonder agents and autopilot. ``argv_json``
    must be a JSON string array. The exact absolute ``project`` directory is the
    only host bind and is read-only unless the host explicitly sets
    ``writable_workspace=true``. Docker/Podman flags, mounts, devices, user,
    environment, sockets, and privileges are not caller-configurable.

    The fixed policy disables networking, uses a read-only root filesystem,
    drops all capabilities, enables no-new-privileges, scrubs the process
    environment, and caps time, output, memory, CPU, PIDs, stdin, and /tmp.
    This relies on the external runtime and host kernel and is not escape-proof.
    """
    _maybe_live_reload()
    started = time.time()
    ok = False
    def deny(code: str, message: str) -> str:
        _record_direct_tool(
            "isolated_run",
            {"denial": code, "writable_workspace": writable_workspace is True},
            ok=False, started=started, summary=code, output=message,
        )
        return message

    account = _admin_account_from_token(token) if token else None
    authorized, _message = admin_auth.require(account, "developer")
    expected = os.environ.get("SONDER_ISOLATED_APPROVAL_CODE", "")
    approval_ok = bool(
        expected and approval and hmac.compare_digest(approval, expected)
    )
    if not authorized or not approval_ok:
        return deny("authorization-denied", (
            "ERROR: isolated_run requires a developer token and the host's "
            "SONDER_ISOLATED_APPROVAL_CODE."
        ))
    if acknowledge_isolation_limits is not True:
        return deny(
            "risk-acknowledgement-denied",
            "ERROR: acknowledge_isolation_limits=true is required.",
        )
    if writable_workspace is True:
        expected_write = os.environ.get("SONDER_ISOLATED_WRITE_APPROVAL_CODE", "")
        if not (
            expected_write
            and write_approval
            and hmac.compare_digest(write_approval, expected_write)
        ):
            return deny("writable-authorization-denied", (
                "ERROR: writable_workspace requires the separate host "
                "SONDER_ISOLATED_WRITE_APPROVAL_CODE."
            ))
    try:
        result = isolated_runner.run_isolated(
            image=image,
            argv_json=argv_json,
            project=project,
            stdin=stdin,
            writable_workspace=writable_workspace,
            timeout=timeout,
            memory_mb=memory_mb,
            cpus=cpus,
            pids=pids,
            output_bytes=output_bytes,
        )
        ok = bool(result.get("ok"))
    except (OSError, ValueError) as exc:
        _record_direct_tool(
            "isolated_run",
            {"failure": "policy-or-runtime-error",
             "writable_workspace": writable_workspace is True},
            ok=False, started=started, summary="isolated runner rejected request",
        )
        return "ERROR: %s" % exc
    output = isolated_runner.format_result(result)
    _record_direct_tool(
        "isolated_run",
        {"image": image, "project": project,
         "writable_workspace": writable_workspace is True},
        ok=ok, started=started, summary=("ok" if ok else "failed"),
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
    target = os.path.abspath(os.path.expanduser(str(path or "")))
    if not os.path.exists(target):
        return "ERROR: artifact path not found: %s" % target
    if not os.path.isdir(target):
        return ("ERROR: artifact path is not a pack directory (expected a "
                "directory containing manifest.json): %s" % target)
    try:
        result = assetgen.verify_pack(path)
    except FileNotFoundError as exc:
        missing = getattr(exc, "filename", None) or os.path.basename(str(exc)) or "a required file"
        return "ERROR: artifact pack is incomplete (missing %s)" % missing
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
            apply=action.get("apply") is True,
        ))
    if action_type == "memory_privacy_review":
        return _loop_text_result("memory_privacy_review", memory_privacy_review(
            sample_limit=action.get("sample_limit", 20),
        ))
    if action_type == "memory_privacy_repair":
        return _loop_text_result("memory_privacy_repair", memory_privacy_repair(
            lesson_ids_json=action.get("lesson_ids", action.get("lesson_ids_json", [])),
            apply=action.get("apply") is True,
        ))
    if action_type == "memory_embedding_backfill":
        return _loop_text_result("memory_embedding_backfill", memory_embedding_backfill(
            limit=action.get("limit", 25), apply=action.get("apply") is True,
        ))
    if action_type == "memory_interaction_embedding_backfill":
        return _loop_text_result(
            "memory_interaction_embedding_backfill",
            memory_interaction_embedding_backfill(
                limit=action.get("limit", 25), apply=action.get("apply") is True,
            ),
        )
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
        capacity_args = {
            "requested_agents": action.get("requested_agents", action.get("agents", 0)),
        }
        if "worker_cap" in action:
            capacity_args["worker_cap"] = action.get("worker_cap")
        return _loop_text_result("master_capacity", master_capacity(**capacity_args))
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
            agents=action.get("agents", 0),
            worker_cap=action.get("worker_cap", 0),
            tier=action.get("tier", "auto"),
            learn=action.get("learn", False),
            project=action.get("project", ""),
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
            risk_policy=action.get("risk_policy", ""),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "artifact_risk_inspect":
        return _loop_text_result("artifact_risk_inspect", artifact_risk_inspect(
            path=action.get("path", ""),
            max_scan_bytes=action.get("max_scan_bytes", 16 * 1024 * 1024),
            max_seconds=action.get("max_seconds", 5.0),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "process_list":
        return _loop_text_result("process_list", process_list(
            max_processes=action.get("max_processes", 128),
            max_seconds=action.get("max_seconds", 0.5),
        ))
    if action_type == "process_memory_risk_inspect":
        return _loop_text_result(
            "process_memory_risk_inspect", process_memory_risk_inspect(
                pid=action.get("pid", 0),
                max_bytes=action.get("max_bytes", 4 * 1024 * 1024),
                max_regions=action.get("max_regions", 256),
                max_seconds=action.get("max_seconds", 1.0),
            ),
        )
    if action_type == "image_inspect":
        return _loop_text_result("image_inspect", image_inspect(
            path=action.get("path", ""),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "data_inspect":
        return _loop_text_result("data_inspect", data_inspect(
            path=action.get("path", ""),
            max_bytes=action.get("max_bytes", 256000),
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
    if action_type == "file_copy":
        return _loop_text_result("file_copy", file_copy(
            source=action.get("source", ""),
            destination=action.get("destination", ""),
            overwrite=action.get("overwrite", False),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_move":
        return _loop_text_result("file_move", file_move(
            source=action.get("source", ""),
            destination=action.get("destination", ""),
            overwrite=action.get("overwrite", False),
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
            recursive=action.get("recursive") is True,
            dry_run=action.get("dry_run") is not False,
            confirm=action.get("confirm", ""),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "self_heal_check":
        return _loop_text_result("self_heal_check", self_heal_check())
    if action_type == "self_heal_repair":
        return _loop_text_result("self_heal_repair", self_heal_repair(
            apply=action.get("apply") is True,
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
        "output": "Valid action types: code, project, artifact_generate, artifact_ground, game_reference_suite, game_generate_and_test, game_generation_campaign, offload, sonder, master_orchestrate, master_status, master_capacity, master_cancel, master_retry, file_policy, workspace_inventory, directory_tree, text_search, script_search, program_search, workspace_run, script_run, image_inspect, file_find, file_read, file_write, file_edit, file_copy, file_move, file_delete, status, diagnostics, context_health, learning_health, memory_quality_report, memory_quality_repair, memory_privacy_review, memory_privacy_repair, memory_embedding_backfill, memory_interaction_embedding_backfill, improvement_report, self_heal_check, self_heal_repair, profile_status, emotion_status, emotion_update, emotion_tune, learn_preference, preferences_status, memory_search, ground_artifact, apply_learned, web_search, web_fetch, weather_lookup, approximate_location_lookup, unload, sleep.",
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
      - {"type":"offload","prompt":"...","tier":"fast|code|general|reasoning|vision|cloud-code|cloud-general"}
      - {"type":"sonder","prompt":"...","session":"none"}
      - {"type":"sonder","prompt":"...","context_size":"1m"}
      - {"type":"master_orchestrate","task":"...","mode":"inline|delegate|fleet","agents":3,"project":"/path/to/repo"}
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
      - {"type":"memory_interaction_embedding_backfill","limit":25,"apply":false}
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
    from sonder_runtime.application.workflows import render_workflow_result

    return render_workflow_result(_application().workflows.list())


@mcp.tool()
def workflow_save(name: str, actions_json: str, description: str = "") -> str:
    """Save a named workflow made of loop action objects.

    `actions_json` may be a JSON list or {"actions": [...]}.
    """
    _maybe_live_reload()
    from sonder_runtime.application.workflows import render_workflow_result

    return render_workflow_result(
        _application().workflows.save(name, actions_json, description)
    )


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
    from sonder_runtime.application.workflows import render_workflow_result

    result = _application().workflows.run(
        name,
        _loop_dispatch,
        max_iterations=max_iterations,
        stop_on_failure=stop_on_failure,
        stop_on_success=stop_on_success,
        delay_seconds=delay_seconds,
    )
    return render_workflow_result(result)


@mcp.tool()
def workflow_delete(name: str) -> str:
    """Delete a saved workflow from workflows.json."""
    _maybe_live_reload()
    from sonder_runtime.application.workflows import render_workflow_result

    return render_workflow_result(_application().workflows.delete(name))


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
    # A bot-block, captcha interstitial, or Access Denied page arrives with a
    # 200 and reads like a document, so without this the caller consumes a
    # refusal as if it were the requested content -- worse than a 404 because
    # it looks like success. Name the denial instead of relaying it.
    blocked = artifact_fetch_module.detect_block_page(
        out, content_type="text/html", url=url,
    )
    if blocked is not None:
        notice = artifact_fetch_module.format_block_notice(url, blocked)
        _record_direct_tool(
            "web_fetch",
            {"url": url, "max_chars": max_chars},
            ok=False, started=started,
            summary="blocked: %s" % blocked.get("reason", "denial page"),
            output=notice,
        )
        return notice
    _record_direct_tool(
        "web_fetch",
        {"url": url, "max_chars": max_chars},
        ok=not out.startswith("ERROR:"), started=started,
        summary="%d chars" % len(out),
        output=out,
    )
    return out


@mcp.tool()
def local_service_probe(
    url: str,
    method: str = "GET",
    timeout: float = 2.0,
) -> str:
    """Probe an unauthenticated HTTP service that resolves only to loopback."""
    _maybe_live_reload()
    started = time.time()
    args = {"url": url, "method": method, "timeout": timeout}
    try:
        result = local_probe.probe(url, method=method, timeout=timeout)
    except Exception as exc:
        _record_direct_tool(
            "local_service_probe", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(result, ensure_ascii=False, sort_keys=True)
    _record_direct_tool(
        "local_service_probe", args, ok=True, started=started,
        summary="HTTP %s in %sms" % (
            result.get("status", "?"), result.get("latency_ms", "?"),
        ),
        output=output,
    )
    return output


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


def _safe_mcp_recovery_action(provenance: dict) -> str:
    if not provenance.get("issue"):
        return ""
    return reloadable_mcp._recovery_action(
        bool(provenance.get("configured_root_ready"))
    )


def _safe_mcp_error(value) -> str:
    text = str(value or "")
    safe_messages = {
        "stale runtime source: loaded MCP file is unavailable",
        "configured runtime root is unavailable",
        "loaded MCP source does not match configured runtime root",
    }
    if text in safe_messages:
        return text
    error_type = text.partition(":")[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", error_type):
        return "%s: source refresh failed" % error_type
    return "runtime source refresh failed"


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
        "  source registration: %s"
        % ("available" if data.get("path") else "unknown"),
        "  loaded/current: %s / %s" % (loaded, current),
    ]
    provenance = data.get("provenance") or {}
    if provenance:
        lines.extend([
            "  process: pid=%s | python=%s"
            % (
                provenance.get("pid", "unknown"),
                "python" if provenance.get("python") else "unknown",
            ),
            "  process cwd: %s"
            % (
                "unavailable"
                if provenance.get("cwd") == "(deleted or unavailable)"
                else "available"
            ),
            "  source root: %s"
            % ("present" if provenance.get("source_root_exists") else "missing"),
            "  configured runtime root: %s"
            % (
                "present"
                if provenance.get("configured_root_exists")
                else "missing/not set"
            ),
        ])
        if provenance.get("issue"):
            lines.append("  provenance ERROR: %s" % provenance["issue"])
        action = _safe_mcp_recovery_action(provenance)
        if action:
            lines.append("  ACTION: %s" % action)
    if data.get("last_refresh_ts"):
        lines.append("  last refresh unix time: %s" % data["last_refresh_ts"])
    if data.get("last_error"):
        lines.append(
            "  ERROR: %s (last known-good registry remains active)"
            % _safe_mcp_error(data["last_error"])
        )
    if data.get("last_notification_error"):
        lines.append("  notification warning: MCP list-change notification failed")
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
    from sonder_runtime.application.preferences import render_preference_result

    return render_preference_result(_application().preferences.learn(text, scope))


@mcp.tool()
def preferences_status(include_disabled: bool = False, limit: int = 50) -> str:
    """List learned user preferences that shape future responses."""
    _maybe_live_reload()
    from sonder_runtime.application.preferences import render_preference_result

    return render_preference_result(
        _application().preferences.status(include_disabled, limit)
    )


def preference_command(arg: str = "") -> str:
    text = (arg or "").strip()
    if not text or text.lower() in ("list", "status", "show"):
        return preferences_status()
    lower = text.lower()
    if lower.startswith("forget "):
        target = text[7:].strip()
        if not target:
            return "usage: /prefer forget <id-or-key>"
        from sonder_runtime.application.preferences import render_preference_result

        return render_preference_result(
            _application().preferences.disable(target)
        )
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
    interaction_id = memory_store.new_id()
    emb = embeddings.embed(task)
    if not embeddings.valid_vector(emb):
        emb = None
    blob = embeddings.to_blob(emb) if emb else None
    provenance = embeddings.provenance(emb) if emb else {}
    # SPEC-3: the example interaction is written through the UnitOfWork-owned
    # MemoryRepository; _DB_PATH keeps the tool on the server's database.
    with _application().unit_of_work(db_path=_DB_PATH) as uow:
        uow.memory.log_interaction(
            interaction_id, task, "", solution, "example",
            task_embedding=blob,
            task_embedding_model=provenance.get("model"),
            task_embedding_revision=provenance.get("revision"),
            task_embedding_dim=provenance.get("dimension"),
        )
    result = _record_outcome_and_maybe_distill(interaction_id, signal)
    if result["lesson_id"]:
        return "Learned lesson %s from example interaction %s." % (
            result["lesson_id"], interaction_id,
        )
    if result["distillation_deferred"]:
        return (
            "Example recorded as %s; lesson distillation was deferred for retry."
            % interaction_id
        )
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
def evaluation_history_status(
    model: str = "",
    model_digest: str = "",
    suite: str = "",
    suite_version: str = "",
    suite_digest: str = "",
    tolerance: float = 0.0,
    max_records: int = 10000,
) -> str:
    """Read identity-separated evaluation trends; never runs or promotes a model."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "model": model, "model_digest": model_digest, "suite": suite,
        "suite_version": suite_version, "suite_digest": suite_digest,
        "tolerance": tolerance, "max_records": max_records,
    }
    try:
        data = eval_history_use_cases.EvaluationHistoryService(
            eval_history_adapter.LegacyEvaluationHistoryReader()
        ).status(
            model=model,
            model_digest=model_digest,
            suite=suite,
            suite_version=suite_version,
            suite_digest=suite_digest,
            tolerance=tolerance,
            max_records=max_records,
        )
    except Exception as exc:
        _record_direct_tool(
            "evaluation_history_status", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool(
        "evaluation_history_status", args, ok=True, started=started,
        summary="%d identity group(s)" % len(data["groups"]), output=output,
    )
    return output


@mcp.tool()
def repo_log(
    path: str = ".",
    revision: str = "HEAD",
    file_path: str = "",
    count: int = 20,
    timeout: float = 5.0,
    max_bytes: int = 256000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Read bounded structured commit history from an exact repository root."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "revision": revision, "file_path": file_path,
        "count": count, "timeout": timeout, "max_bytes": max_bytes,
    }
    try:
        data = git_history.repo_log(
            path,
            revision=revision,
            file_path=file_path,
            count=count,
            timeout=timeout,
            max_bytes=max_bytes,
            extra_roots=(
                extra_roots if _file_bypass_allowed(token, approval) else ""
            ),
        )
    except Exception as exc:
        _record_direct_tool(
            "repo_log", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool(
        "repo_log", args, ok=True, started=started,
        summary="%d commit(s)" % data["count"], output=output,
    )
    return output


@mcp.tool()
def repo_show(
    path: str = ".",
    revision: str = "HEAD",
    file_path: str = "",
    timeout: float = 5.0,
    max_bytes: int = 256000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Read one bounded commit patch for a required safe contained file."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "revision": revision, "file_path": file_path,
        "timeout": timeout, "max_bytes": max_bytes,
    }
    try:
        data = git_history.repo_show(
            path,
            revision=revision,
            file_path=file_path,
            timeout=timeout,
            max_bytes=max_bytes,
            extra_roots=(
                extra_roots if _file_bypass_allowed(token, approval) else ""
            ),
        )
    except Exception as exc:
        _record_direct_tool(
            "repo_show", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool(
        "repo_show", args, ok=True, started=started,
        summary=data["commit"][:12], output=output,
    )
    return output


@mcp.tool()
def repo_blame(
    path: str = ".",
    file_path: str = "",
    revision: str = "HEAD",
    start_line: int = 1,
    end_line: int = 0,
    timeout: float = 5.0,
    max_bytes: int = 256000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Read bounded structured blame records for one contained regular file."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "file_path": file_path, "revision": revision,
        "start_line": start_line, "end_line": end_line,
        "timeout": timeout, "max_bytes": max_bytes,
    }
    try:
        data = git_history.repo_blame(
            path,
            file_path=file_path,
            revision=revision,
            start_line=start_line,
            end_line=end_line,
            timeout=timeout,
            max_bytes=max_bytes,
            extra_roots=(
                extra_roots if _file_bypass_allowed(token, approval) else ""
            ),
        )
    except Exception as exc:
        _record_direct_tool(
            "repo_blame", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = json.dumps(data, indent=2, sort_keys=True)
    _record_direct_tool(
        "repo_blame", args, ok=True, started=started,
        summary="%d line(s)" % data["count"], output=output,
    )
    return output


@mcp.tool()
def tool_manifest() -> str:
    """List the sonder-runtime MCP tools and what they are for."""
    tools = {
        "agent": "Run a Claude-like tool-calling loop that can use local tools and web tools. Exact-ack unsafe lab mode removes its host tool policy only on a loopback, unprivileged process.",
        "autopilot_start/autopilot_status/autopilot_resume/autopilot_pause/autopilot_cancel": "Run a restart-persistent local goal with evidence-aware checkpoints, bounded replans, host tool gates, and explicit lifecycle control.",
        "runtime_policy_status/runtime_policy_update": "Inspect or guarded-edit shared hot-reloadable local model mappings and execution-lane tiers; cloud opt-in stays separate.",
        "mcp_runtime_status/live_reload_status": "Audit atomic MCP source/tool convergence, refresh history, list-change signaling, and fail-closed reload errors.",
        "master_orchestrate/master_status/master_capacity/master_cancel/master_retry": "Run restart-safe hardware-scheduled orchestration, inspect capacity/activity, cancel fleets, and explicitly retry interrupted work.",
        "admin_register/admin_login/admin_accounts/admin_set_account": "Manage hosted accounts, roles, bans, tiers, and developer flags.",
        "admin_status/debug_inspect/admin_private_chain_of_thought": "Inspect admin/debug state and safely deny private chain-of-thought exposure.",
        "sonder": "Ask through Sonder Runtime's local learning loop.",
        "offload": "Route a self-contained task to a configured local/cloud tier.",
        "web_search/web_fetch/weather_lookup/approximate_location_lookup": "Search/fetch public pages, get sourced weather, or resolve an explicitly consented approximate IP location without retaining the IP.",
        "local_service_probe": "Bounded unauthenticated GET/HEAD health probe for an explicit-port HTTP/HTTPS service resolving exclusively to loopback.",
        "workspace_inventory/workspace_compare/dependency_inventory/directory_tree/directory_create/text_search/file_read_range/context_pack": "Budgeted guarded workspace/dependency inventory and metadata-only comparison, folder discovery, creation, text search, bounded line-range reads, and multi-file context packs.",
        "repo_status/repo_diff": "Inspect bounded read-only Git branch, worktree, staged, and unstaged state without shell execution.",
        "project_detect": "Inventory guarded build/test/runtime manifests and return deterministic evidence-backed language, framework, and cross-platform argv candidates without executing them.",
        "file_policy/file_find/file_read/file_write/file_batch_write/json_patch/file_edit/file_copy/file_move/file_delete/text_patch": "Guarded filesystem find/read/create/edit/transactional batch write/atomic JSON patch/single-file transfer/delete and strict unified-diff preview/apply.",
        "repository_symbol_index": "Build a deterministic bounded read-only declaration index with Python AST and conservative JS/TS/C/C++/C#/Rust/Go extraction.",
        "repo_log/repo_show/repo_blame": "Read bounded structured Git history, patches, and line attribution from an exact project repository without shell execution or upward discovery.",
        "file_digest/directory_digest": "Stream guarded files into SHA-256 and build deterministic relative-path manifests with fail-closed complete or explicitly partial directory Merkle roots.",
        "archive_list/archive_extract": "Prevalidate bounded ZIP/TAR manifests or transactionally extract them to a new non-overwriting workspace directory.",
        "archive_create": "Transactionally create a bounded deterministic ZIP/TAR from explicit guarded project inputs without overwriting.",
        "artifact_risk_inspect": "Statically inspect guarded PDFs, PE/ELF/Mach-O executables, scripts, or opaque binaries for bounded risk indicators without executing or returning content.",
        "process_list/process_memory_risk_inspect": "Opt-in bounded Windows process metadata and fixed-indicator memory-risk inspection; never returns command lines, paths, addresses, strings, or raw bytes.",
        "log_inspect": "Inspect one guarded text log with fixed level/timestamp/source extraction, failure clusters, repeats, and bounded context.",
        "scaffold_project": "Write a complete deterministic project skeleton (cpp-msvc .sln/.vcxproj, cpp-cmake, csharp, rust, python, node, typescript, go, java-maven) -- never hand-write solution/build plumbing.",
        "environment_status": "Report the host OS, available shells (PowerShell/cmd/bash/wsl), and installed toolchains -- check before choosing a command shape or assuming a tool exists.",
        "hardware_profile": "Detect cross-vendor accelerators and report conservative resident, unified-memory, and GPU+RAM-spill model plans without changing host settings.",
        "data_inspect/data_query/sqlite_mutate": "Preview structured data, run bounded read-only queries, or explicitly preview/apply one guarded parameterized SQLite DML statement.",
        "data_convert": "Preview or atomically create a non-overwriting JSON/JSONL/CSV/TSV conversion with explicit ordered fields.",
        "program_search/script_search/workspace_run/script_run/image_inspect": "Discover installed programs and workspace scripts, run bounded argv-only processes, and inspect image metadata; script_run applies the operator execution-risk policy before launch.",
        "task_create/task_list/task_update/task_show/task_delete/task_plan/task_progress/task_depend/checklist_create/checklist_update/checklist_show": "Visible todo and ordered checklist state shared by console, app, agents, and MCP. task_plan batch-creates a work plan with ordered steps and auto-dependencies. task_progress shows a compact summary. task_depend manages blocking relationships.",
        "workbench_agent": "Run an autonomous local tool loop with a guaranteed checklist, exact action transcript, validation gate, and end report.",
        "command_registry_list": "Inspect available slash commands by category, name, or risk.",
        "activity_status": "Inspect active/latest response activity, tool calls, and file changes.",
        "permission_policy/permission_rule_set": "Inspect or guarded-edit local permission rules for tool actions.",
        "context_compaction_plan": "Preview when to summarize, split sessions, or reduce live context.",
        "run_code": "Run a bounded snippet: Python, JS/TypeScript, Bash, Ruby, Perl, PHP, Lua, R, Go, Java, Rust, PowerShell, C++, C#.",
        "isolated_run": "Direct MCP-only, explicitly enabled and developer-authorized Docker/Podman execution with approved roots, separate writable approval, and a fixed resource-capped isolation policy.",
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
        "evaluation_history_status": "Read explicit evaluation trends separated by exact model digest and suite version/digest; it never runs or promotes a model.",
        "memory_quality_report/memory_quality_repair": "Audit and dry-run/prune exact duplicate lessons.",
        "memory_privacy_review/memory_privacy_repair": "Review redacted privacy findings and explicitly dry-run/remove selected flagged lessons.",
        "memory_embedding_backfill": "Dry-run or refresh stale/missing semantic vectors with the local embedding model.",
        "memory_interaction_embedding_backfill": "Dry-run or locally refresh stale raw-interaction task vectors without printing task text.",
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
- workspace_compare: {"left": "path/to/left", "right": "path/to/right", "max_entries": 2000, "max_file_bytes": 64000000, "max_total_bytes": 256000000, "max_details": 1000, "max_output_bytes": 256000, "timeout": 5}
- repo_status: {"root": ".", "timeout": 10, "max_output": 128000}
- repo_diff: {"root": ".", "staged": false, "path": "", "context": 3, "timeout": 10, "max_output": 128000}
- project_detect: {"path": ".", "max_depth": 8, "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_results": 500}
- dependency_inventory: {"path": ".", "max_depth": 5, "max_files": 100, "max_total_bytes": 2000000, "max_results": 2000}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- directory_create: {"path": "output/reports", "parents": true}
- file_find: {"query": "*.py", "root": ".", "max_results": 50}
- repository_symbol_index: {"path": ".", "glob": "*", "language": "auto|python|javascript|typescript|c|cpp|csharp|rust|go", "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_symbols": 2000}
- file_read: {"path": "README.md"}
- file_digest: {"path": "artifact.bin", "max_bytes": 32000000}
- directory_digest: {"path": ".", "max_depth": 12, "max_files": 2000, "max_total_bytes": 32000000, "max_file_bytes": 32000000, "max_results": 2500}
- file_read_range: {"path": "server.py", "start_line": 1, "end_line": 200}
- context_pack: {"paths_json": ["README.md", "src/main.py"], "max_files": 12, "max_total_bytes": 256000, "max_bytes_per_file": 64000}
- repo_log: {"path": ".", "revision": "HEAD", "file_path": "", "count": 20, "timeout": 5, "max_bytes": 256000}
- repo_show: {"path": ".", "revision": "HEAD", "file_path": "<required contained relative file>", "timeout": 5, "max_bytes": 256000}
- repo_blame: {"path": ".", "file_path": "<contained relative file>", "revision": "HEAD", "start_line": 1, "end_line": 100, "timeout": 5, "max_bytes": 256000}
- archive_list: {"path": "bundle.zip", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100, "max_results": 2500}
- archive_extract: {"source": "bundle.zip", "destination": "unpacked", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100} -- creates a new directory; never overwrites
- archive_create: {"root": ".", "inputs_json": ["src", "README.md"], "destination": "release.zip", "archive_format": "zip|tar", "deterministic": true} -- destination must be new and outside input directories
- artifact_risk_inspect: {"path": "artifact.exe", "max_scan_bytes": 16777216, "max_seconds": 5} -- static indicators only; no content execution or raw content return
- process_list: {"max_processes": 128, "max_seconds": 0.5} -- requires exact host opt-in; names/PIDs/counts only
- process_memory_risk_inspect: {"pid": 1234, "max_bytes": 4194304, "max_regions": 256, "max_seconds": 1} -- fixed aggregate indicators only; never raw memory
- log_inspect: {"path": "logs/app.log", "tail_lines": 0, "context_lines": 2, "max_file_bytes": 64000000, "max_scan_bytes": 4000000, "max_lines": 10000, "max_line_bytes": 4096, "max_results": 100, "max_output_bytes": 256000, "timeout": 5}
- text_search: {"query": "TODO", "root": ".", "glob": "*.py", "regex": false, "max_results": 100}
- file_write: {"path": "notes.txt", "content": "...", "mode": "create|overwrite|append"}
- file_batch_write: {"operations_json": [{"path": "a.txt", "content": "...", "mode": "create|overwrite"}]}
- json_patch: {"path": "config.json", "operations_json": [{"op": "test", "path": "/version", "value": 1}, {"op": "replace", "path": "/version", "value": 2}], "mode": "preview|apply"}
- text_patch: {"root": ".", "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n", "apply": false}
- file_edit: {"path": "notes.txt", "old": "before", "new": "after", "count": 1}
- file_copy: {"source": "assets/input.bin", "destination": "build/input.bin", "overwrite": false}
- file_move: {"source": "build/draft.bin", "destination": "dist/final.bin", "overwrite": false}
- file_delete: {"path": "notes.txt", "dry_run": true}
- scaffold_project: {"kind": "cpp-msvc|cpp-cmake|csharp|rust|python|node|typescript|go|java-maven", "name": "MyApp", "root": "MyApp"} -- writes the full skeleton (.sln/.vcxproj/Cargo.toml/...); use this instead of hand-writing build/solution files
- environment_status: {} -- host OS, shells, installed toolchains; check before choosing command shapes
- hardware_profile: {"workload": "general|chat|coding|agentic|research", "refresh": false} -- cross-vendor device inventory and conservative local-model fit; detection is not backend readiness
- script_search: {"query": "build", "root": ".", "max_results": 100}
- program_search: {"query": "python", "max_results": 50}
- workspace_run: {"program": "git", "args_json": ["status", "--short"], "cwd": ".", "timeout": 30}
- script_run: {"path": "scripts/check.py", "args_json": [], "cwd": ".", "timeout": 30, "risk_policy": "off|report|deny-high|deny-medium|deny-unknown"} -- request may strengthen but never weaken operator policy
- test_discover: {"root": ".", "framework": "auto"} -- discover tests; auto-detects pytest/jest/vitest/cargo/go/dotnet
- test_run: {"root": ".", "framework": "auto", "path": "", "pattern": "", "verbose": false, "coverage": false, "timeout": 120, "extra_args_json": "[]"} -- run tests with filtering, coverage, extra args
- lint_run: {"root": ".", "tool": "auto", "path": "", "fix": false, "timeout": 60} -- lint with ruff/flake8/eslint/clippy; fix=true to auto-fix
- format_code: {"root": ".", "tool": "auto", "path": "", "check_only": false, "timeout": 60} -- format with ruff/black/prettier/rustfmt/gofmt
- typecheck_run: {"root": ".", "tool": "auto", "path": "", "timeout": 120} -- type check with mypy/pyright/tsc
- dependency_add: {"root": ".", "packages_json": "[\"requests\"]", "dev": false, "timeout": 60} -- install packages with auto-detected manager
- dependency_remove: {"root": ".", "packages_json": "[\"requests\"]", "timeout": 60} -- uninstall packages
- dependency_update: {"root": ".", "packages_json": "[]", "timeout": 120} -- update packages (empty = all)
- dependency_audit: {"root": ".", "timeout": 60} -- audit for vulnerabilities
- git_commit: {"root": ".", "message": "fix: description", "paths_json": "[\"src/main.py\"]", "all_tracked": false, "timeout": 30} -- commit staged or specified files
- git_branch: {"root": ".", "name": "feature/x", "checkout": true, "base": "", "timeout": 10} -- create and checkout a branch
- git_checkout: {"root": ".", "ref": "main", "timeout": 10} -- switch branch/tag/commit
- git_stash: {"root": ".", "action": "push|pop|list|drop", "message": "", "include_untracked": true, "timeout": 10}
- git_tag: {"root": ".", "name": "v1.0.0", "message": "", "delete": false, "timeout": 10}
- git_merge: {"root": ".", "branch": "feature/x", "no_ff": true, "message": "", "timeout": 30}
- git_cherry_pick: {"root": ".", "commits_json": "[\"abc123\"]", "timeout": 30}
- build_run: {"root": ".", "command": "", "timeout": 120} -- auto-detects Make/Cargo/CMake/Go/npm/Gradle/Maven; command overrides
- build_clean: {"root": ".", "timeout": 30} -- clean build artifacts
- rename_symbol: {"root": ".", "old_name": "foo", "new_name": "bar", "glob": "**/*.py", "dry_run": true} -- rename across files; dry_run=false to apply
- find_references: {"root": ".", "symbol": "MyClass", "glob": "**/*.py"} -- find all occurrences of a symbol
- diff_files: {"root": ".", "left": "a.py", "right": "b.py", "context": 3} -- unified diff between two files
- apply_patch: {"root": ".", "patch_text": "...", "check_only": false} -- apply a unified diff patch
- secret_scan: {"root": ".", "timeout": 30} -- scan for leaked API keys, passwords, tokens, private keys
- image_inspect: {"path": "artifacts/generated/demo/icon.png"}
- data_inspect: {"path": "data/records.jsonl", "max_bytes": 256000}
- data_query: {"path": "data/records.jsonl", "sql": "", "projection_json": ["id", "/nested/name"], "filters_json": {"status": "active"}, "max_rows": 100, "max_columns": 50, "max_output_bytes": 256000, "max_scan_bytes": 4000000, "timeout": 5}
- data_convert: {"input_path": "data/records.jsonl", "output_path": "data/records.csv", "fields_json": ["id", "name"], "output_format": "csv", "apply": false, "max_input_bytes": 16000000, "max_output_bytes": 16000000, "max_rows": 10000, "max_columns": 100, "max_fields": 50, "max_field_bytes": 64000, "max_depth": 16, "preview_rows": 5, "timeout": 10}
- sqlite_mutate: {"path": "data/app.db", "sql": "UPDATE records SET status = ? WHERE id = ?", "parameters_json": ["done", 42], "mode": "preview|apply", "max_rows": 1000, "timeout": 2, "max_db_bytes": 67108864}
- task_create: {"title": "...", "detail": "...", "priority": 2, "project": "...", "owner": "..."}
- task_list: {"status": "pending|in_progress|blocked|done|canceled", "project": "", "include_done": false, "limit": 50}
- task_update: {"task_id": "...", "status": "in_progress|blocked|done", "note": "..."}
- task_show: {"task_id": "..."}
- task_delete: {"task_id": "..."}
- task_plan: {"title": "...", "steps": ["Step 1", "Step 2", {"title": "Step 3", "detail": "..."}], "project": "...", "sequential": true}
- task_progress: {"project": ""}
- task_depend: {"task_id": "...", "depends_on": "...", "remove": false}
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
- evaluation_history_status: {"model": "", "model_digest": "", "suite": "", "suite_version": "", "suite_digest": "", "tolerance": 0.0, "max_records": 10000}
- context_policy_status: {"context_size": "1m"}
- set_context_size: {"context_size": "256k"}
- memory_quality_report: {"sample_limit": 5}
- memory_quality_repair: {"apply": false}
- memory_privacy_review: {"sample_limit": 20}
- memory_privacy_repair: {"lesson_ids_json": ["lesson-id"], "apply": false}
- memory_embedding_backfill: {"limit": 25, "apply": false}
- memory_interaction_embedding_backfill: {"limit": 25, "apply": false}
- system_improvement_report: {}
- master_orchestrate: {"task": "...", "mode": "ask|inline|delegate|fleet", "agents": 24, "worker_cap": 24, "tier": "code", "project": "/path/to/repo"} -- worker_cap is a per-run override bounded by the operator ceiling
- master_status: {}
- master_capacity: {"requested_agents": 0, "worker_cap": 0}
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
- offload: {"prompt": "...", "tier": "fast|code|general|reasoning|vision|cloud-code|cloud-general"}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""


REPOSITORY_READ_ONLY_TOOLS = frozenset({
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "directory_digest",
    "file_read_range", "context_pack",
    "repo_status", "repo_diff",
    "repo_log", "repo_show", "repo_blame",
    "project_detect", "data_inspect", "data_query", "archive_list", "artifact_risk_inspect",
    "verify_artifact",
    "text_search", "script_search", "program_search", "image_inspect", "command_registry_list",
    "activity_status", "permission_policy", "context_compaction_plan",
    "diagnostics", "context_health", "learning_health_status", "context_policy_status", "artifact_ground",
    "evaluation_history_status",
    "memory_quality_report", "memory_privacy_review", "system_improvement_report", "master_status", "master_capacity",
    "self_heal_check", "status", "system_profile_text", "environment_status", "hardware_profile",
    "emotion_vector_status", "preferences_status", "tool_manifest",
    "memory_search", "web_search", "web_fetch", "weather_lookup",
    "test_discover", "find_references", "diff_files", "secret_scan",
})
REPOSITORY_READ_ONLY_FORBIDDEN_ARGS = frozenset({
    "token", "approval", "extra_roots",
})
REPOSITORY_AGENT_TOOL_HELP = """Available tools:
The JSON values below are schema placeholders, not suggested filenames or
search terms. Replace every <...> value with an exact task-relevant path,
symbol, filename, or glob. For a code/symbol audit, start with text_search for
an exact symbol named by the task; do not default to Python or server.py.
- file_policy: {}
- workspace_inventory: {"path": ".", "max_entries": 20000, "timeout_seconds": 10, "top_n": 15}
- workspace_compare: {"left": "<first task-relevant file or directory>", "right": "<second task-relevant file or directory>", "max_entries": 2000, "max_file_bytes": 64000000, "max_total_bytes": 256000000, "max_details": 1000, "max_output_bytes": 256000, "timeout": 5}
- repo_status: {"root": ".", "timeout": 10, "max_output": 128000}
- repo_diff: {"root": ".", "staged": false, "path": "", "context": 3, "timeout": 10, "max_output": 128000}
- project_detect: {"path": ".", "max_depth": 8, "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_results": 500}
- dependency_inventory: {"path": ".", "max_depth": 5, "max_files": 100, "max_total_bytes": 2000000, "max_results": 2000}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- file_find: {"query": "<task-relevant filename or glob>", "root": ".", "max_results": 50}
- repository_symbol_index: {"path": ".", "glob": "<task-relevant source glob>", "language": "auto|python|javascript|typescript|c|cpp|csharp|rust|go", "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_symbols": 2000}
- file_read: {"path": "<task-relevant relative path>", "max_bytes": 256000}
- file_digest: {"path": "<task-relevant relative file>", "max_bytes": 32000000}
- directory_digest: {"path": ".", "max_depth": 12, "max_files": 2000, "max_total_bytes": 32000000, "max_file_bytes": 32000000, "max_results": 2500}
- file_read_range: {"path": "<task-relevant relative path>", "start_line": 1, "end_line": 200}
- context_pack: {"paths_json": ["<task-relevant relative path>", "<another task-relevant relative path>"], "max_files": 12, "max_total_bytes": 256000, "max_bytes_per_file": 64000}
- repo_log: {"path": ".", "revision": "HEAD", "file_path": "<optional contained relative path>", "count": 20, "timeout": 5, "max_bytes": 256000}
- repo_show: {"path": ".", "revision": "HEAD", "file_path": "<required contained relative file>", "timeout": 5, "max_bytes": 256000}
- repo_blame: {"path": ".", "file_path": "<required contained relative file>", "revision": "HEAD", "start_line": 1, "end_line": 100, "timeout": 5, "max_bytes": 256000}
- archive_list: {"path": "<task-relevant ZIP or TAR>", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100, "max_results": 2500}
- artifact_risk_inspect: {"path": "<task-relevant document, executable, script, or binary>", "max_scan_bytes": 16777216, "max_seconds": 5}
- verify_artifact: {"path": "<task-relevant downloaded installer, archive, or image>", "expect_type": "pe|msi|zip|iso|elf", "expect_publisher": "<required signer substring>", "sha256": "<64-hex digest>"}
- log_inspect: {"path": "<task-relevant log file>", "tail_lines": 0, "context_lines": 2, "max_file_bytes": 64000000, "max_scan_bytes": 4000000, "max_lines": 10000, "max_line_bytes": 4096, "max_results": 100, "max_output_bytes": 256000, "timeout": 5}
- text_search: {"query": "<exact task symbol or anchor>", "root": ".", "glob": "<task-relevant glob>", "max_results": 100}
- script_search: {"query": "<task-relevant script name>", "root": ".", "max_results": 100}
- program_search: {"query": "<required program name>", "max_results": 50}
- environment_status: {"refresh": false}
- hardware_profile: {"workload": "general|chat|coding|agentic|research", "refresh": false}
- image_inspect: {"path": "<task-relevant image path>"}
- data_inspect: {"path": "<task-relevant data file>", "max_bytes": 256000}
- data_query: {"path": "<task-relevant SQLite, JSON, JSONL, CSV, or TSV file>", "sql": "<SQLite SELECT/CTE only, otherwise empty>", "projection_json": ["<field or JSON pointer>"], "filters_json": {"<field or JSON pointer>": "<exact value>"}, "max_rows": 100, "max_columns": 50, "max_output_bytes": 256000, "max_scan_bytes": 4000000, "timeout": 5}
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
- evaluation_history_status: {"model": "", "model_digest": "", "suite": "", "suite_version": "", "suite_digest": "", "tolerance": 0.0, "max_records": 10000}
- artifact_ground: {"path": "artifacts/generated/report", "recipe": "auto", "requirements_json": {}}
- context_policy_status: {"context_size": "32k"}
- memory_quality_report: {"sample_limit": 5}
- memory_privacy_review: {"sample_limit": 20}
- system_improvement_report: {"session": "", "project": ""}
- master_status: {}
- master_capacity: {"requested_agents": 0, "worker_cap": 0}
- self_heal_check: {}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- preferences_status: {"include_disabled": false, "limit": 20}
- tool_manifest: {}
- test_discover: {"root": ".", "framework": "auto"} -- discover tests; auto-detects pytest/jest/vitest/cargo/go/dotnet
- find_references: {"root": ".", "symbol": "MyClass", "glob": "**/*.py"} -- find all occurrences of a symbol
- diff_files: {"root": ".", "left": "a.py", "right": "b.py", "context": 3} -- unified diff between two files
- secret_scan: {"root": ".", "timeout": 30} -- scan for leaked API keys, passwords, tokens, private keys

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""


def _agent_tool_help(read_only=False, cloud=False):
    help_text = REPOSITORY_AGENT_TOOL_HELP if read_only else AGENT_TOOL_HELP
    if not cloud:
        return help_text
    return "\n".join(
        line for line in help_text.splitlines()
        if not any(
            line.lstrip().startswith("- %s:" % name)
            for name in _CLOUD_AGENT_LOCAL_ONLY_TOOLS
        )
    )


def _tool_capability_shadow_surfaces():
    """Snapshot authoritative tool surfaces for opt-in drift validation."""
    manager = getattr(mcp, "_tool_manager", None)
    registered = getattr(manager, "_tools", {})
    direct_names = frozenset(registered) if isinstance(registered, dict) else frozenset()
    dispatch_tools = tool_capabilities.dispatch_names(_agent_dispatch)
    return tool_capabilities.ShadowSurfaces(
        direct_mcp_tools=direct_names,
        tool_manifest=tool_manifest(),
        repository_read_only_tools=REPOSITORY_READ_ONLY_TOOLS,
        project_bound_agent_tools=_PROJECT_BOUND_AGENT_TOOLS,
        project_scoped_tools=_PROJECT_SCOPED_PATH_TOOLS | _PROJECT_SCOPED_EXECUTION_TOOLS,
        dispatch_tools=dispatch_tools,
        hosted_agent_tools=(
            dispatch_tools
            - _CLOUD_AGENT_NESTED_MODEL_TOOLS
            - _CLOUD_AGENT_LOCAL_ONLY_TOOLS
        ),
        deduplicated_inspection_tools=_AGENT_DEDUPLICATED_INSPECTION_TOOLS,
        work_inspection_tools=_WORK_INSPECTION_TOOLS,
        full_agent_help=AGENT_TOOL_HELP,
        repository_agent_help=REPOSITORY_AGENT_TOOL_HELP,
        hosted_agent_help=_agent_tool_help(cloud=True),
    )


def tool_capability_shadow_report():
    """Validate descriptor drift without making descriptors authoritative."""
    return tool_capabilities.format_shadow_report(_tool_capability_shadow_surfaces())


def _repository_scope_path_error(tool_name, args, project_root):
    """Reject a project-bound agent path outside its host-selected root.

    Generic guarded reads authorize both Sonder's workspace and configured
    extra roots.  Repository agents need a narrower contract: when a project is
    bound, even another normally authorized root (especially Sonder's own cwd)
    is out of scope.
    """
    scoped_tools = _PROJECT_SCOPED_PATH_TOOLS | _PROJECT_SCOPED_EXECUTION_TOOLS
    if not project_root or tool_name not in scoped_tools:
        return ""
    try:
        root = Path(str(project_root)).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project root is not a directory")
        if tool_name == "workspace_run":
            targets = [("working directory", args.get("cwd") or ".")]
        elif tool_name == "script_run":
            targets = [("script path", args.get("path") or "")]
            if str(args.get("cwd") or "").strip():
                targets.append(("working directory", args.get("cwd")))
        elif tool_name == "data_convert":
            targets = [
                ("input path", args.get("input_path") or ""),
                ("output path", args.get("output_path") or ""),
            ]
        elif tool_name == "repo_diff":
            targets = [("repository root", args.get("root") or ".")]
            if str(args.get("path") or "").strip():
                targets.append(("diff path", args.get("path")))
        elif tool_name == "file_batch_write":
            operations = _batch_agent_operations(args)
            if operations is None or not operations:
                return "ERROR: agent project path rejected: operations must be a non-empty JSON list"
            targets = []
            for index, operation in enumerate(operations):
                if not isinstance(operation, dict):
                    return "ERROR: agent project path rejected: operation %d must be an object" % index
                targets.append(("operation %d path" % index, operation.get("path") or ""))
        elif tool_name == "context_pack":
            targets = [
                ("path", path)
                for path in _context_pack_paths(
                    args.get("paths_json", args.get("paths", []))
                )
            ]
        elif tool_name == "archive_create":
            archive_root = Path(str(args.get("root") or ".")).expanduser()
            if not archive_root.is_absolute():
                archive_root = root / archive_root
            targets = [("archive root", archive_root)]
            try:
                archive_inputs = archive_create_tool._parse_inputs(
                    args.get("inputs_json", args.get("inputs", []))
                )
            except ValueError as exc:
                return "ERROR: agent project archive inputs rejected: %s" % exc
            for raw_input in archive_inputs:
                candidate = Path(raw_input).expanduser()
                targets.append((
                    "archive input",
                    candidate if candidate.is_absolute() else archive_root / candidate,
                ))
            destination_text = str(args.get("destination") or "").strip()
            if not destination_text:
                targets.append(("archive destination", ""))
            else:
                raw_destination = Path(destination_text).expanduser()
                targets.append((
                    "archive destination",
                    raw_destination if raw_destination.is_absolute()
                    else archive_root / raw_destination,
                ))
        elif tool_name in {"file_copy", "file_move"}:
            targets = [
                ("source", args.get("source") or ""),
                ("destination", args.get("destination") or ""),
            ]
        elif tool_name == "archive_extract":
            targets = [
                ("archive source", args.get("source") or ""),
                ("archive destination", args.get("destination") or ""),
            ]
        elif tool_name == "text_patch":
            targets = [("root", args.get("root") or ".")]
            try:
                targets.extend(
                    ("patch path", item["path"])
                    for item in text_patch_ops._parse(args.get("patch", ""))
                )
            except (TypeError, ValueError, PermissionError) as exc:
                return "ERROR: agent project path rejected: invalid patch: %s" % exc
        elif tool_name == "workspace_compare":
            targets = [
                ("left path", args.get("left") or ""),
                ("right path", args.get("right") or ""),
            ]
        else:
            key = _project_scoped_path_key(tool_name)
            targets = [("path", args.get(key) or ".")]
        for label, raw_value in targets:
            raw = str(raw_value or "").strip()
            if not raw:
                return "ERROR: agent project path rejected: %s is required" % label
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = root / target
            if label == "diff path":
                # Preserve a tracked symlink as the lexical Git pathspec while
                # still resolving and confining every parent component.
                target = target.parent.resolve(strict=False) / target.name
            else:
                target = target.resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                return (
                    "ERROR: agent project path rejected: %s is outside the "
                    "host-selected project root" % label
                )
    except (OSError, TypeError, ValueError) as exc:
        return "ERROR: agent project scope is invalid: %s" % exc
    return ""


def _agent_project_execution_argument_error(tool_name, args, project_root):
    """Reject explicit project-execution escape paths and inline interpreters.

    This is a host argument guard, not an OS sandbox: an allowed project
    program can still access resources available to the user account.  Keep
    that limitation explicit while blocking the direct escape forms a model
    can otherwise express in workspace/script argv.
    """
    if (
        not project_root
        or tool_name not in _PROJECT_SCOPED_EXECUTION_TOOLS
        or not isinstance(args, dict)
    ):
        return ""
    root = Path(str(project_root)).expanduser().resolve(strict=True)
    argv = _agent_argv(args)
    lowered = [item.casefold() for item in argv]
    program = Path(str(args.get("program") or "")).name.casefold()
    inline_flags = {
        "python": {"-c"}, "python.exe": {"-c"},
        "py": {"-c"}, "py.exe": {"-c"},
        "node": {"-e", "--eval", "-p", "--print"},
        "node.exe": {"-e", "--eval", "-p", "--print"},
        "bash": {"-c"}, "bash.exe": {"-c"},
        "sh": {"-c"}, "sh.exe": {"-c"},
        "perl": {"-e"}, "perl.exe": {"-e"},
        "ruby": {"-e"}, "ruby.exe": {"-e"},
        "powershell": {"-command", "-c", "-encodedcommand", "-enc"},
        "powershell.exe": {"-command", "-c", "-encodedcommand", "-enc"},
        "pwsh": {"-command", "-c", "-encodedcommand", "-enc"},
        "pwsh.exe": {"-command", "-c", "-encodedcommand", "-enc"},
        "cmd": {"/c", "/k"}, "cmd.exe": {"/c", "/k"},
    }
    forbidden = inline_flags.get(program, set())
    if re.fullmatch(r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?", program):
        forbidden = {"-c"}
    elif re.fullmatch(r"node(?:js)?(?:\d+(?:\.\d+)*)?(?:\.exe)?", program):
        forbidden = {"-e", "--eval", "-p", "--print"}
    if forbidden.intersection(lowered):
        return (
            "ERROR: agent project execution rejected: inline interpreter "
            "commands are outside the project path guard"
        )
    # Catch common launchers such as `uv run python3 -c ...` as well as a
    # directly selected interpreter.
    for index, token in enumerate(lowered[:-1]):
        child = Path(token).name
        child_flags = set()
        if re.fullmatch(r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?", child):
            child_flags = {"-c"}
        elif re.fullmatch(r"node(?:js)?(?:\d+(?:\.\d+)*)?(?:\.exe)?", child):
            child_flags = {"-e", "--eval", "-p", "--print"}
        if any(item in child_flags for item in lowered[index + 1:]):
            return (
                "ERROR: agent project execution rejected: inline interpreter "
                "commands are outside the project path guard"
            )
    # An interpreter runs the program on its STDIN whenever argv names no
    # script to run. `-` says so explicitly, but it is not the only way and was
    # the only one checked here: `python`, `bash`, `sh` and `node` with EMPTY
    # argv all read a program from stdin and execute it, so requiring "-" in
    # argv let every inline-code control in this guard be walked around by
    # simply omitting the dash.
    #
    # The operand test is what keeps this from over-blocking. `python script.py`
    # with stdin attached is a program reading DATA, which is legitimate and
    # must stay allowed; only an invocation with no operand -- bare, or
    # flags-only like `python -u`, or an explicit `-` -- makes stdin the code.
    # A token starting with "-" is a flag (or the dash itself), never an
    # operand.
    if forbidden and str(args.get("stdin") or ""):
        has_script_operand = any(
            token and not token.startswith("-") for token in lowered
        )
        if not has_script_operand:
            return (
                "ERROR: agent project execution rejected: interpreter code via "
                "stdin is outside the project path guard"
            )

    base_value = args.get("cwd") or (
        os.path.dirname(str(args.get("path") or "")) if tool_name == "script_run" else "."
    )
    base = Path(str(base_value)).expanduser().resolve(strict=False)
    for raw_item in argv:
        raw = str(raw_item or "").strip().strip('"\'')
        if not raw:
            continue
        if "=" in raw and raw.startswith(("-", "/")):
            raw = raw.split("=", 1)[1].strip().strip('"\'')
        raw = raw.lstrip("@")
        drive_match = re.search(r"[A-Za-z]:[\\/]", raw)
        unc_match = re.search(r"\\\\[^\\]+\\[^\\]+", raw)
        if drive_match:
            raw = raw[drive_match.start():]
        elif unc_match:
            raw = raw[unc_match.start():]
        # On Windows, /Zi and /W4 are compiler switches, not POSIX paths.
        if (
            os.name == "nt"
            and raw.startswith("/")
            and "\\" not in raw
            and raw.count("/") == 1
        ):
            continue
        path_like = (
            os.path.isabs(raw)
            or raw.startswith((".", "~"))
            or "/" in raw
            or "\\" in raw
            or bool(os.path.splitext(raw)[1])
        )
        if not path_like:
            continue
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = base / target
        target = target.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            return (
                "ERROR: agent project execution rejected: argv path is outside "
                "the host-selected project root"
            )
    return ""


def _repository_read_only_error(tool_name, args, trusted_extra_roots=""):
    if not isinstance(args, dict):
        return "ERROR: repository read-only tool args must be a JSON object."
    if tool_name not in REPOSITORY_READ_ONLY_TOOLS:
        return "ERROR: tool '%s' is not allowed by the repository read-only policy." % tool_name
    if tool_name in _GIT_IGNORE_DISCOVERY_TOOLS and args.get("include_ignored"):
        return (
            "ERROR: repository read-only tool '%s' forbids include_ignored=true."
            % tool_name
        )
    forbidden = sorted(
        name for name in REPOSITORY_READ_ONLY_FORBIDDEN_ARGS.intersection(args)
        if not (
            name == "extra_roots"
            and trusted_extra_roots
            and args.get("extra_roots") == trusted_extra_roots
        )
    )
    if forbidden:
        return (
            "ERROR: repository read-only tool '%s' forbids argument(s): %s."
            % (tool_name, ", ".join(forbidden))
        )
    scope_error = _repository_scope_path_error(
        tool_name, args, trusted_extra_roots,
    )
    if scope_error:
        return scope_error
    try:
        if tool_name in {"file_read", "file_digest", "file_read_range", "image_inspect", "data_inspect", "data_query", "log_inspect", "artifact_risk_inspect"}:
            file_ops.resolve_repository_read_path(
                args.get("path", ""),
                allow_workspace_root=False,
                reject_sensitive=True,
                extra_roots=trusted_extra_roots,
            )
        elif tool_name in {"repo_status", "repo_diff"}:
            root_value = args.get("root", "") or "."
            resolved_root = file_ops.resolve_repository_read_path(
                root_value,
                allow_workspace_root=True,
                reject_sensitive=True,
                extra_roots=trusted_extra_roots,
            )
            diff_path = str(args.get("path") or "").strip()
            if tool_name == "repo_diff" and diff_path:
                git_tools._resolve_diff_path(
                    resolved_root, diff_path,
                    extra_roots=trusted_extra_roots,
                )
        elif tool_name == "context_pack":
            for path in _context_pack_paths(args.get("paths_json", args.get("paths", []))):
                file_ops.resolve_repository_read_path(
                    path,
                    allow_workspace_root=False,
                    reject_sensitive=True,
                    extra_roots=trusted_extra_roots,
                )
        elif tool_name in {"repo_log", "repo_show", "repo_blame"}:
            root = git_history.resolve_repo_root(
                args.get("path", "."), extra_roots=trusted_extra_roots,
            )
            git_history.validate_revision(args.get("revision", "HEAD"))
            if tool_name == "repo_blame":
                git_history.resolve_blame_target(root, args.get("file_path", ""))
                git_history.normalize_blame_range(
                    args.get("start_line", 1), args.get("end_line", 0),
                )
            elif tool_name == "repo_show":
                git_history.resolve_show_target(root, args.get("file_path", ""))
            else:
                git_history.resolve_path_filter(root, args.get("file_path", ""))
        elif tool_name == "archive_list":
            file_ops.resolve_repository_read_path(
                args.get("path", ""), allow_workspace_root=False,
                reject_sensitive=True, extra_roots=trusted_extra_roots,
            )
        elif tool_name == "workspace_compare":
            for path in (args.get("left", ""), args.get("right", "")):
                file_ops.resolve_repository_read_path(
                    path, allow_workspace_root=True, reject_sensitive=True,
                    extra_roots=trusted_extra_roots,
                )
        elif tool_name in {"workspace_inventory", "dependency_inventory", "project_detect", "directory_digest", "directory_tree", "file_find", "repository_symbol_index", "text_search", "script_search"}:
            file_ops.resolve_repository_read_path(
                args.get("path", "") or args.get("root", "") or ".",
                allow_workspace_root=True,
                reject_sensitive=True,
                extra_roots=trusted_extra_roots,
            )
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        return "ERROR: repository read-only path rejected: %s" % exc
    return ""


def _extract_agent_json(text):
    """Parse an agent decision, tolerating markdown fences and prose framing.

    Small local models wrap decisions in ```json fences or surround them
    with commentary; a balanced-brace scan recovers the first complete JSON
    object instead of failing on trailing text. Genuinely truncated JSON
    still raises so the decision-repair loop can re-prompt.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        # Drop the opening fence line and any closing fence.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("agent response was not JSON: %s" % text[:300])
    # Balanced scan: find the first complete top-level object, ignoring
    # braces inside JSON strings, so prose after the object cannot break
    # parsing the way rfind("}") could.
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError("agent response was not JSON: %s" % text[:300])


_AGENT_OBSERVATION_PROMPT_CHARS = 9000
_AGENT_DECISION_REPAIR_LIMIT = 2
_AGENT_NEGATIVE_CLAIM_RE = re.compile(
    r"\b(?:does not|doesn't|did not|could not|cannot|can't)\s+"
    r"(?:contain|include|find|locate|exist)\b|"
    r"\b(?:not found|no matches?|none found|missing from)\b|"
    # "There are no .cpp files", "contains no source files", "no such file",
    # "found no results" -- existence denials phrased around a SEARCHED-FOR
    # artifact (files/matches/functions/symbols/...), which the plain
    # "no matches"/"does not exist" forms above missed. A workbench agent
    # answering "There are no .cpp files" (while its own directory listing
    # showed 44) sailed past this guard because none of the original phrasings
    # matched. Scoped to concrete search artifacts so ordinary negatives ("no
    # errors", "no changes needed", "no side effects") do NOT trigger a
    # re-verification pass.
    r"\bno\s+(?:such\s+)?(?:[\w.*-]+\s+){0,2}"
    r"(?:files?|matches?|results?|occurrences?|instances?|entries|entry|"
    r"functions?|methods?|classes|class|symbols?|references?|definitions?|"
    r"declarations?|usages?|hits?|records?|rows?|directories|directory|folders?)\b",
    re.IGNORECASE,
)
_AGENT_CLAIM_REVIEW_TOOLS = frozenset({
    "text_search", "file_read_range", "file_find", "repository_symbol_index", "project_detect",
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
    length_limited = False
    try:
        raw = gen(step_prompt)
    except ModelCallError as error:
        # Cancellation is a control-flow signal used by fleet callers and must
        # retain its stable exception identity.  Hosted/local transport
        # failures, however, are ordinary agent outcomes: return them to the
        # agent boundary instead of leaking an MCP traceback.
        if error.kind == "cancelled":
            raise
        length_limited = (
            error.kind == "empty_response"
            and '"done_reason": "length"' in error.detail
            and repair_limit > 0
        )
        if not length_limited:
            return None, "", error
        raw = ""
    length_limited = length_limited or (
        getattr(gen, "last_response_meta", {}).get("done_reason") == "length"
    )
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
        recovery = ""
        if length_limited:
            recovery = (
                "\nHOST LENGTH RECOVERY: the previous decision hit its output "
                "limit. Do not repeat the oversized response. For a large "
                "file_write, emit one valid chunk of at most %d characters now "
                "and use append on a later tool turn; otherwise simplify the "
                "decision."
                % _CLOUD_AGENT_WRITE_CHUNK_HINT
            )
        repair_prompt = (
            step_prompt
            + "\n\nHOST FORMAT REPAIR %d/%d: Your previous response was invalid. "
            "Return exactly one JSON object and no prose or Markdown. Use %s.\n"
            "Parser error: %s%s\nPrevious response excerpt:\n%s"
            % (
                attempt + 1,
                repair_limit,
                valid_shape,
                error,
                recovery,
                str(raw or "")[:1000],
            )
        )
        try:
            raw = gen(repair_prompt)
            length_limited = (
                getattr(gen, "last_response_meta", {}).get("done_reason")
                == "length"
            )
        except ModelCallError as model_error:
            if model_error.kind == "cancelled":
                raise
            if (
                model_error.kind == "empty_response"
                and '"done_reason": "length"' in model_error.detail
                and attempt + 1 < repair_limit
            ):
                length_limited = True
                raw = ""
                continue
            return None, raw, model_error
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
    cancel_check=None,
    cloud_budget_state=None,
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
    if cloud and cloud_budget_state is None:
        cloud_budget_state = {
            "spent": 0,
            "total": _CLOUD_AGENT_OUTPUT_BUDGET,
        }
    gen = _make_generate(
        model, system, 0.0, 260, 4096, cloud=cloud,
        cancel_check=cancel_check, compact_cloud_reasoning=True,
    )
    if cloud and cloud_budget_state is not None:
        gen = _bounded_cloud_agent_generate(
            gen,
            per_call_limit=260,
            total_budget=int(
                cloud_budget_state.get("total", _CLOUD_AGENT_OUTPUT_BUDGET)
            ),
            budget_state=cloud_budget_state,
        )
    correction = ""
    last_error = "invalid claim review"
    for _attempt in range(2):
        try:
            raw = gen(review_prompt + correction)
        except ModelCallError as error:
            if error.kind == "cancelled":
                raise
            return {
                "decision": "error",
                "reason": _format_model_call_error(error),
                "tool": "",
                "args": {},
            }
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
    repository_extra_roots="",
):
    unsafe = unsafe_lab.active()
    if unsafe:
        # The acknowledgement is specifically permission to remove model-loop
        # policy.  Direct MCP tool contracts remain explicit and unchanged.
        allow_web = True
        read_only = False
        allow_location = True
        repository_extra_roots = ""
    tool_name = (tool_name or "").strip()
    args = args or {}
    if not isinstance(args, dict):
        return "ERROR: tool args must be a JSON object"
    if read_only:
        if repository_extra_roots:
            # Defense in depth for direct/internal dispatch callers.  The
            # observed agent path already scopes before policy, but dispatch
            # itself must not let a relative path fall back to Sonder's cwd.
            args = _project_scope_args(
                tool_name, args, repository_extra_roots,
            )
        policy_error = _repository_read_only_error(
            tool_name, args, trusted_extra_roots=repository_extra_roots,
        )
        if policy_error:
            return policy_error
        if tool_name in {"command_registry_list", "tool_manifest"}:
            return _agent_tool_help(read_only=True)
        if repository_extra_roots:
            # The model cannot grant itself filesystem authority.  Replace any
            # scoped value with the exact host-selected project root, then use
            # an in-process-only approval sentinel for the guarded read tools.
            args = dict(args)
            args["extra_roots"] = repository_extra_roots
            args["approval"] = _TRUSTED_REPOSITORY_APPROVAL
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
    if tool_name == "repo_status":
        return repo_status(
            root=args.get("root", "."),
            timeout=args.get("timeout", 10),
            max_output=args.get("max_output", 128000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "repo_diff":
        return repo_diff(
            root=args.get("root", "."),
            staged=args.get("staged") is True,
            path=args.get("path", ""),
            context=args.get("context", 3),
            timeout=args.get("timeout", 10),
            max_output=args.get("max_output", 128000),
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
    if tool_name == "project_detect":
        return project_detect(
            path=args.get("path", args.get("root", ".")),
            max_depth=args.get("max_depth", 8),
            max_files=args.get("max_files", 200),
            max_total_bytes=args.get("max_total_bytes", 2_000_000),
            max_file_bytes=args.get("max_file_bytes", 256_000),
            max_results=args.get("max_results", 500),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "dependency_inventory":
        return dependency_inventory(
            path=args.get("path", args.get("root", ".")),
            max_depth=args.get("max_depth", 5),
            max_files=args.get("max_files", 100),
            max_total_bytes=args.get("max_total_bytes", 2000000),
            max_results=args.get("max_results", 2000),
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
            include_ignored=args.get("include_ignored", False),
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
            include_ignored=args.get("include_ignored", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "repository_symbol_index":
        return repository_symbol_index(
            path=args.get("path", args.get("root", ".")),
            glob=args.get("glob", "*"),
            language=args.get("language", ""),
            max_files=args.get("max_files", 200),
            max_total_bytes=args.get("max_total_bytes", 2_000_000),
            max_file_bytes=args.get("max_file_bytes", 256_000),
            max_symbols=args.get("max_symbols", 2_000),
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
    if tool_name == "context_pack":
        return context_pack(
            paths_json=args.get("paths_json", args.get("paths", [])),
            max_files=args.get("max_files", 12),
            max_total_bytes=args.get("max_total_bytes", 256000),
            max_bytes_per_file=args.get("max_bytes_per_file", 64000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "data_inspect":
        return data_inspect(
            path=args.get("path", ""),
            max_bytes=args.get("max_bytes", 256000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "data_query":
        return data_query(
            path=args.get("path", ""),
            sql=args.get("sql", ""),
            projection_json=(
                json.dumps(args.get("projection_json"))
                if not isinstance(args.get("projection_json", "[]"), str)
                else args.get("projection_json", "[]")
            ),
            filters_json=(
                json.dumps(args.get("filters_json"))
                if not isinstance(args.get("filters_json", "{}"), str)
                else args.get("filters_json", "{}")
            ),
            max_rows=args.get("max_rows", 100),
            max_columns=args.get("max_columns", 50),
            max_output_bytes=args.get("max_output_bytes", 256000),
            max_scan_bytes=args.get("max_scan_bytes", 4000000),
            timeout=args.get("timeout", 5.0),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "data_convert":
        fields = args.get("fields_json", args.get("fields", []))
        if not isinstance(fields, str):
            fields = json.dumps(fields, ensure_ascii=False)
        return data_convert(
            input_path=args.get("input_path", ""),
            output_path=args.get("output_path", ""),
            fields_json=fields,
            output_format=args.get("output_format", ""),
            apply=args.get("apply", False),
            max_input_bytes=args.get("max_input_bytes", 16_000_000),
            max_output_bytes=args.get("max_output_bytes", 16_000_000),
            max_rows=args.get("max_rows", 10_000),
            max_columns=args.get("max_columns", 100),
            max_fields=args.get("max_fields", 50),
            max_field_bytes=args.get("max_field_bytes", 64_000),
            max_depth=args.get("max_depth", 16),
            preview_rows=args.get("preview_rows", 5),
            timeout=args.get("timeout", 10.0),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "sqlite_mutate":
        parameters = args.get("parameters_json", args.get("parameters", []))
        if not isinstance(parameters, str):
            parameters = json.dumps(parameters, ensure_ascii=False)
        return sqlite_mutate(
            path=args.get("path", ""), sql=args.get("sql", ""),
            parameters_json=parameters, mode=args.get("mode", "preview"),
            max_rows=args.get("max_rows", 1000), timeout=args.get("timeout", 2.0),
            max_db_bytes=args.get("max_db_bytes", 67108864),
            token=args.get("token", ""), approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "log_inspect":
        return log_inspect(
            path=args.get("path", ""),
            tail_lines=args.get("tail_lines", 0),
            context_lines=args.get("context_lines", 2),
            max_file_bytes=args.get("max_file_bytes", 64000000),
            max_scan_bytes=args.get("max_scan_bytes", 4000000),
            max_lines=args.get("max_lines", 10000),
            max_line_bytes=args.get("max_line_bytes", 4096),
            max_results=args.get("max_results", 100),
            max_output_bytes=args.get("max_output_bytes", 256000),
            timeout=args.get("timeout", 5.0),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "workspace_compare":
        return workspace_compare(
            left=args.get("left", ""),
            right=args.get("right", ""),
            max_entries=args.get("max_entries", 2000),
            max_file_bytes=args.get("max_file_bytes", 64000000),
            max_total_bytes=args.get("max_total_bytes", 256000000),
            max_details=args.get("max_details", 1000),
            max_output_bytes=args.get("max_output_bytes", 256000),
            timeout=args.get("timeout", 5.0),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "repo_log":
        return repo_log(
            path=args.get("path", "."),
            revision=args.get("revision", "HEAD"),
            file_path=args.get("file_path", ""),
            count=args.get("count", 20),
            timeout=args.get("timeout", 5.0),
            max_bytes=args.get("max_bytes", 256000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "repo_show":
        return repo_show(
            path=args.get("path", "."),
            revision=args.get("revision", "HEAD"),
            file_path=args.get("file_path", ""),
            timeout=args.get("timeout", 5.0),
            max_bytes=args.get("max_bytes", 256000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "repo_blame":
        return repo_blame(
            path=args.get("path", "."),
            file_path=args.get("file_path", ""),
            revision=args.get("revision", "HEAD"),
            start_line=args.get("start_line", 1),
            end_line=args.get("end_line", 0),
            timeout=args.get("timeout", 5.0),
            max_bytes=args.get("max_bytes", 256000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "archive_list":
        return archive_list(
            path=args.get("path", ""),
            max_entries=args.get("max_entries", archive_tools.DEFAULT_MAX_ENTRIES),
            max_file_bytes=args.get("max_file_bytes", archive_tools.DEFAULT_MAX_FILE_BYTES),
            max_total_bytes=args.get("max_total_bytes", archive_tools.DEFAULT_MAX_TOTAL_BYTES),
            max_ratio=args.get("max_ratio", archive_tools.DEFAULT_MAX_RATIO),
            max_path_depth=args.get("max_path_depth", archive_tools.DEFAULT_MAX_PATH_DEPTH),
            max_results=args.get("max_results", archive_tools.DEFAULT_MAX_RESULTS),
            max_seconds=args.get("max_seconds", archive_tools.DEFAULT_MAX_SECONDS),
            token=args.get("token", ""), approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "archive_extract":
        return archive_extract(
            source=args.get("source", ""), destination=args.get("destination", ""),
            max_entries=args.get("max_entries", archive_tools.DEFAULT_MAX_ENTRIES),
            max_file_bytes=args.get("max_file_bytes", archive_tools.DEFAULT_MAX_FILE_BYTES),
            max_total_bytes=args.get("max_total_bytes", archive_tools.DEFAULT_MAX_TOTAL_BYTES),
            max_ratio=args.get("max_ratio", archive_tools.DEFAULT_MAX_RATIO),
            max_path_depth=args.get("max_path_depth", archive_tools.DEFAULT_MAX_PATH_DEPTH),
            max_seconds=args.get("max_seconds", archive_tools.DEFAULT_MAX_SECONDS),
            token=args.get("token", ""), approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "archive_create":
        inputs = args.get("inputs_json", args.get("inputs", []))
        if not isinstance(inputs, str):
            inputs = json.dumps(inputs, ensure_ascii=False)
        return archive_create(
            root=args.get("root", "."), inputs_json=inputs,
            destination=args.get("destination", ""),
            archive_format=args.get("archive_format", args.get("format", "zip")),
            deterministic=args.get("deterministic", True),
            max_files=args.get("max_files", archive_create_tool.DEFAULT_MAX_FILES),
            max_entries=args.get("max_entries", archive_create_tool.DEFAULT_MAX_ENTRIES),
            max_file_bytes=args.get("max_file_bytes", archive_create_tool.DEFAULT_MAX_FILE_BYTES),
            max_total_bytes=args.get("max_total_bytes", archive_create_tool.DEFAULT_MAX_TOTAL_BYTES),
            max_depth=args.get("max_depth", archive_create_tool.DEFAULT_MAX_DEPTH),
            max_results=args.get("max_results", archive_create_tool.DEFAULT_MAX_RESULTS),
            token=args.get("token", ""), approval=args.get("approval", ""),
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
    if tool_name == "file_digest":
        return file_digest(
            path=args.get("path", ""),
            max_bytes=args.get("max_bytes", 32_000_000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "directory_digest":
        return directory_digest(
            path=args.get("path", args.get("root", ".")),
            max_depth=args.get("max_depth", 12),
            max_files=args.get("max_files", 2_000),
            max_total_bytes=args.get("max_total_bytes", 32_000_000),
            max_file_bytes=args.get("max_file_bytes", 32_000_000),
            max_results=args.get("max_results", 2_500),
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
    if tool_name == "file_copy":
        return file_copy(
            source=args.get("source", ""),
            destination=args.get("destination", ""),
            overwrite=args.get("overwrite", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_move":
        return file_move(
            source=args.get("source", ""),
            destination=args.get("destination", ""),
            overwrite=args.get("overwrite", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_batch_write":
        operations = args.get("operations_json", args.get("operations", []))
        if not isinstance(operations, str):
            operations = json.dumps(operations, ensure_ascii=False)
        return file_batch_write(
            operations_json=operations,
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "json_patch":
        operations = args.get("operations_json", args.get("operations", []))
        if not isinstance(operations, str):
            operations = json.dumps(operations, ensure_ascii=False)
        return json_patch(
            path=args.get("path", ""),
            operations_json=operations,
            mode=args.get("mode", "preview"),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "text_patch":
        return text_patch(
            root=args.get("root", "."), patch=args.get("patch", ""),
            apply=args.get("apply") is True, token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "scaffold_project":
        return scaffold_project(
            kind=args.get("kind", ""),
            name=args.get("name", ""),
            root=args.get("root", ""),
            apply=bool(args.get("apply", True)),
        )
    if tool_name == "environment_status":
        return environment_status(refresh=bool(args.get("refresh", False)))
    if tool_name == "hardware_profile":
        return hardware_profile(
            workload=args.get("workload", "general"),
            refresh=bool(args.get("refresh", False)),
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
            risk_policy=args.get("risk_policy", ""),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "artifact_risk_inspect":
        return artifact_risk_inspect(
            path=args.get("path", ""),
            max_scan_bytes=args.get("max_scan_bytes", 16 * 1024 * 1024),
            max_seconds=args.get("max_seconds", 5.0),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "verify_artifact":
        return verify_artifact(
            path=args.get("path", ""),
            expect_type=args.get("expect_type", ""),
            expect_publisher=args.get("expect_publisher", ""),
            sha256=args.get("sha256", ""),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "process_list":
        return process_list(
            max_processes=args.get("max_processes", 128),
            max_seconds=args.get("max_seconds", 0.5),
        )
    if tool_name == "process_memory_risk_inspect":
        return process_memory_risk_inspect(
            pid=args.get("pid", 0),
            max_bytes=args.get("max_bytes", 4 * 1024 * 1024),
            max_regions=args.get("max_regions", 256),
            max_seconds=args.get("max_seconds", 1.0),
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
    if tool_name == "task_delete":
        return task_delete(args.get("task_id", args.get("id", "")))
    if tool_name == "task_plan":
        steps = args.get("steps", args.get("items", []))
        return task_plan(
            title=args.get("title", "Work plan"),
            steps=json.dumps(steps) if not isinstance(steps, str) else steps,
            project=args.get("project", ""),
            owner=args.get("owner", "agent"),
            priority=args.get("priority", 2),
            sequential=args.get("sequential", True),
        )
    if tool_name == "task_progress":
        return task_progress(project=args.get("project", ""))
    if tool_name == "task_depend":
        return task_depend(
            task_id=args.get("task_id", args.get("id", "")),
            depends_on=args.get("depends_on", ""),
            remove=args.get("remove", False),
        )
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
    if tool_name == "evaluation_history_status":
        return evaluation_history_status(
            model=args.get("model", ""),
            model_digest=args.get("model_digest", ""),
            suite=args.get("suite", ""),
            suite_version=args.get("suite_version", ""),
            suite_digest=args.get("suite_digest", ""),
            tolerance=args.get("tolerance", 0.0),
            max_records=args.get("max_records", 10000),
        )
    if tool_name == "context_policy_status":
        return context_policy_status(args.get("context_size", ""))
    if tool_name == "set_context_size":
        return set_context_size(args.get("context_size", ""))
    if tool_name == "memory_quality_report":
        return memory_quality_report(sample_limit=args.get("sample_limit", 5))
    if tool_name == "memory_quality_repair":
        return memory_quality_repair(apply=args.get("apply") is True)
    if tool_name == "memory_privacy_review":
        return memory_privacy_review(sample_limit=args.get("sample_limit", 20))
    if tool_name == "memory_privacy_repair":
        return memory_privacy_repair(
            lesson_ids_json=args.get("lesson_ids_json", args.get("lesson_ids", [])),
            apply=args.get("apply") is True,
        )
    if tool_name == "memory_embedding_backfill":
        return memory_embedding_backfill(
            limit=args.get("limit", 25), apply=args.get("apply") is True,
        )
    if tool_name == "memory_interaction_embedding_backfill":
        return memory_interaction_embedding_backfill(
            limit=args.get("limit", 25), apply=args.get("apply") is True,
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
        capacity_args = {
            "requested_agents": args.get("requested_agents", args.get("agents", 0)),
        }
        if "worker_cap" in args:
            capacity_args["worker_cap"] = args.get("worker_cap")
        return master_capacity(**capacity_args)
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
            agents=args.get("agents", 0),
            worker_cap=args.get("worker_cap", 0),
            tier=args.get("tier", "auto"),
            learn=args.get("learn", False),
            project=args.get("project", ""),
        )
    if tool_name == "self_heal_check":
        return self_heal_check()
    if tool_name == "self_heal_repair":
        return self_heal_repair(apply=args.get("apply") is True)
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
    if tool_name == "file_batch_write":
        operations = _batch_agent_operations(args) or []
        return json.dumps(
            [item.get("path", "") for item in operations if isinstance(item, dict)],
            ensure_ascii=False,
        )
    if tool_name == "workspace_compare":
        return "%s | %s" % (args.get("left", ""), args.get("right", ""))
    if tool_name == "data_convert":
        return "%s -> %s" % (
            args.get("input_path", ""), args.get("output_path", ""),
        )
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
    if tool_name in {"file_copy", "file_move"}:
        return "%s -> %s" % (
            args.get("source", ""), args.get("destination", ""),
        )
    if tool_name == "local_service_probe":
        return "%s %s" % (
            str(args.get("method", "GET")).upper(), args.get("url", ""),
        )
    if tool_name == "process_memory_risk_inspect":
        return "pid=%s" % args.get("pid", "")
    if tool_name == "process_list":
        return "max_processes=%s" % args.get("max_processes", 128)
    path = args.get("path") or args.get("root") or ""
    if path:
        return str(path)
    if args.get("query"):
        return "query=%s" % args["query"]
    return ""


_PROJECT_SCOPED_PATH_TOOLS = frozenset({
    "file_read", "file_digest", "directory_digest", "file_read_range", "context_pack", "workspace_compare",
    "repo_log", "repo_show", "repo_blame",
    "data_inspect", "data_query", "data_convert", "sqlite_mutate", "image_inspect", "log_inspect", "file_write", "file_batch_write", "json_patch", "file_edit", "text_patch",
    "archive_list", "archive_extract",
    "file_delete", "directory_create", "workspace_inventory", "dependency_inventory", "directory_tree",
    "file_find", "repository_symbol_index", "text_search", "script_search", "artifact_verify",
    "fetch_artifact", "verify_artifact",
    "artifact_ground", "artifact_risk_inspect", "scaffold_project", "archive_create", "repo_status", "repo_diff", "project_detect", "file_copy", "file_move",
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag", "git_merge", "git_cherry_pick",
    "build_run", "build_clean",
    "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
})
_PROJECT_SCOPED_EXECUTION_TOOLS = frozenset({"workspace_run", "script_run"})
_AGENT_TOOL_ALIASES = {
    "assetgen": "artifact_generate",
    "game_generate": "game_generate_and_test",
    "game_campaign": "game_generation_campaign",
    "improvement_report": "system_improvement_report",
    "agent_status": "master_status",
    "agent_capacity": "master_capacity",
    "agent_cancel": "master_cancel",
    "agent_retry": "master_retry",
    "master": "master_orchestrate",
}
_PROJECT_BOUND_AGENT_TOOLS = (
    _PROJECT_SCOPED_PATH_TOOLS
    | _PROJECT_SCOPED_EXECUTION_TOOLS
    | frozenset({
        "ground_artifact", "program_search",
        "web_search", "web_fetch",
        "weather_lookup", "approximate_location_lookup", "memory_search",
        "file_policy", "task_create", "task_list", "task_update", "task_show",
        "task_delete", "task_plan", "task_progress", "task_depend",
        "checklist_create", "checklist_update", "checklist_show",
        "command_registry_list", "tool_manifest", "activity_status",
        "permission_policy", "context_compaction_plan", "diagnostics",
        "context_health", "learning_health_status", "memory_quality_report",
        "memory_privacy_review", "evaluation_history_status",
        "system_improvement_report", "master_status",
        "master_capacity", "self_heal_check", "status", "system_profile_text",
        "emotion_vector_status", "preferences_status", "context_policy_status",
        "environment_status", "hardware_profile",
        "process_list", "process_memory_risk_inspect",
    })
)
_CLOUD_AGENT_NESTED_MODEL_TOOLS = frozenset({
    "offload", "master_orchestrate", "master_retry", "workflow_run",
    "game_reference_suite", "game_generate_and_test", "game_generation_campaign",
})
_CLOUD_AGENT_LOCAL_ONLY_TOOLS = frozenset({
    "environment_status", "hardware_profile", "file_policy",
    "workspace_inventory", "directory_tree", "file_find", "file_read",
    "file_read_range", "file_digest", "text_search", "repo_status",
    "repo_diff", "artifact_risk_inspect", "process_list",
    "process_memory_risk_inspect",
})


def _cloud_agent_tool_policy_error(tool_name, *, unsafe=False):
    """Keep host-data denial absolute; unsafe bypasses nested models only."""
    if tool_name in _CLOUD_AGENT_LOCAL_ONLY_TOOLS:
        return (
            "ERROR: HOST POLICY: local-only tool '%s' is disabled inside a "
            "hosted agent so private workspace or machine data cannot enter "
            "the hosted model transcript." % tool_name
        )
    if tool_name in _CLOUD_AGENT_NESTED_MODEL_TOOLS:
        if unsafe is True:
            return ""
        return (
            "ERROR: HOST POLICY: nested model-spawning tool '%s' is disabled "
            "inside a hosted agent so all hosted output remains in one "
            "bounded ledger." % tool_name
        )
    return ""


def _canonical_agent_tool_name(tool_name):
    name = str(tool_name or "")
    return _AGENT_TOOL_ALIASES.get(name, name)


def _project_scoped_path_key(tool_name):
    if tool_name == "archive_extract":
        return "destination"
    if tool_name in {
        "file_find", "text_search", "script_search", "scaffold_project",
        "repo_status", "repo_diff", "text_patch", "archive_create",
    }:
        return "root"
    return "path"


def _batch_agent_operations(args):
    value = args.get("operations_json", args.get("operations", []))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, list) else None


def _project_scope_args(tool_name, args, project):
    """Scope an agent file/inspection tool call to the run's project directory.

    When a caller passes project=<dir> to the agent/workbench surface, its file
    tools must actually inspect that directory. Previously `project` was wired
    only to the checklist namespace, so relative paths (".", "VERSIONS.txt")
    resolved against Sonder's own workspace and the agent returned confidently
    wrong "not found"/"empty" answers for the real project. Here we (1) authorize
    the project dir for the call and (2) rebase a relative or omitted path/root
    onto it. The guard (allowed_roots / resolve_*) still validates the final
    path, so this grants nothing outside the authorized project root.
    """
    if (
        not project
        or not isinstance(args, dict)
        or tool_name not in (
            _PROJECT_SCOPED_PATH_TOOLS | _PROJECT_SCOPED_EXECUTION_TOOLS
        )
    ):
        return args
    scoped = dict(args)
    # Never compose a model-supplied root with the trusted host root.  This is
    # the host-resolved path boundary; child processes remain user-level code,
    # not an operating-system sandbox.
    scoped["extra_roots"] = project
    if tool_name == "text_patch":
        # This sentinel is injected only after the host binds the call to its
        # selected project. A model-supplied approval never reaches this path.
        scoped["approval"] = _TRUSTED_REPOSITORY_APPROVAL

    if tool_name == "data_convert":
        for key in ("input_path", "output_path"):
            raw_path = str(scoped.get(key) or "").strip()
            is_abs = os.path.isabs(raw_path) or bool(
                re.match(r"^[A-Za-z]:[\\/]", raw_path)
            )
            if raw_path and not is_abs:
                scoped[key] = os.path.normpath(os.path.join(project, raw_path))
        return scoped

    if tool_name == "file_batch_write":
        operations = _batch_agent_operations(scoped)
        if operations is None:
            return scoped
        rebased = []
        for operation in operations:
            if not isinstance(operation, dict):
                rebased.append(operation)
                continue
            item = dict(operation)
            raw_path = str(item.get("path") or "").strip()
            is_abs = os.path.isabs(raw_path) or bool(
                re.match(r"^[A-Za-z]:[\\/]", raw_path)
            )
            if raw_path and not is_abs:
                item["path"] = os.path.normpath(os.path.join(project, raw_path))
            rebased.append(item)
        scoped["operations_json"] = json.dumps(rebased, ensure_ascii=False)
        scoped.pop("operations", None)
        return scoped

    if tool_name == "context_pack":
        try:
            paths = _context_pack_paths(
                scoped.get("paths_json", scoped.get("paths", []))
            )
        except ValueError:
            # Let the repository policy produce the normal structured error;
            # project rebasing must not turn malformed model output into an
            # uncaught agent-loop exception.
            return scoped
        rebased = []
        for raw_path in paths:
            is_abs = os.path.isabs(raw_path) or bool(
                re.match(r"^[A-Za-z]:[\\/]", raw_path)
            )
            rebased.append(raw_path if is_abs else os.path.join(project, raw_path))
        scoped["paths_json"] = rebased
        scoped.pop("paths", None)
        return scoped

    if tool_name in {"file_copy", "file_move", "archive_extract"}:
        for key in ("source", "destination"):
            raw_path = str(scoped.get(key) or "").strip()
            is_abs = os.path.isabs(raw_path) or bool(
                re.match(r"^[A-Za-z]:[\\/]", raw_path)
            )
            if raw_path and not is_abs:
                scoped[key] = os.path.join(project, raw_path)
        return scoped

    if tool_name == "workspace_compare":
        for key in ("left", "right"):
            raw_path = str(scoped.get(key) or "").strip()
            is_abs = os.path.isabs(raw_path) or bool(
                re.match(r"^[A-Za-z]:[\\/]", raw_path)
            )
            if raw_path and not is_abs:
                scoped[key] = os.path.join(project, raw_path)
        return scoped

    if tool_name == "workspace_run":
        raw_cwd = str(scoped.get("cwd") or ".").strip()
        is_abs = os.path.isabs(raw_cwd) or bool(
            re.match(r"^[A-Za-z]:[\\/]", raw_cwd)
        )
        scoped["cwd"] = raw_cwd if is_abs else os.path.join(project, raw_cwd)
        return scoped

    if tool_name == "script_run":
        raw_path = str(scoped.get("path") or "").strip()
        path_is_abs = os.path.isabs(raw_path) or bool(
            re.match(r"^[A-Za-z]:[\\/]", raw_path)
        )
        if raw_path and not path_is_abs:
            scoped["path"] = os.path.join(project, raw_path)
        raw_cwd = str(scoped.get("cwd") or "").strip()
        cwd_is_abs = os.path.isabs(raw_cwd) or bool(
            re.match(r"^[A-Za-z]:[\\/]", raw_cwd)
        )
        if raw_cwd and not cwd_is_abs:
            scoped["cwd"] = os.path.join(project, raw_cwd)
        return scoped

    key = _project_scoped_path_key(tool_name)
    raw = str(scoped.get(key) or "").strip()
    is_abs = os.path.isabs(raw) or bool(re.match(r"^[A-Za-z]:[\\/]", raw))
    if not raw or raw == ".":
        scoped[key] = project
    elif not is_abs:
        scoped[key] = os.path.join(project, raw)
    return scoped


def _agent_dispatch_observed(
    tool_name, args, allow_web=True, read_only=False, allow_location=False,
    project="",
):
    started = time.time()
    ok = False
    observation = ""
    args = _project_scope_args(tool_name, args, project)
    dispatch_args = args
    if project and tool_name in {"archive_create", "sqlite_mutate"}:
        # Project scope is selected by the host. Grant only that exact root
        # through the unforgeable in-process approval sentinel, while keeping
        # credentials out of the activity record and model-visible arguments.
        dispatch_args = dict(args)
        dispatch_args["approval"] = _TRUSTED_REPOSITORY_APPROVAL
    try:
        with activity_tracker.tool_dispatch_context():
            dispatch_options = {"allow_web": allow_web}
            if allow_location:
                dispatch_options["allow_location"] = True
            if read_only:
                observation = _agent_dispatch(
                    tool_name, dispatch_args, read_only=True,
                    repository_extra_roots=project, **dispatch_options,
                )
            else:
                observation = _agent_dispatch(tool_name, dispatch_args, **dispatch_options)
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
    "directory_create", "file_write", "file_batch_write", "json_patch", "file_edit", "file_copy", "file_move", "file_delete", "text_patch", "data_convert",
    "sqlite_mutate", "scaffold_project", "archive_extract", "archive_create",
    "fetch_artifact",
    "artifact_generate", "game_generate_and_test", "game_generation_campaign",
    "memory_quality_repair", "memory_privacy_repair", "memory_embedding_backfill",
    "memory_interaction_embedding_backfill",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "dependency_add", "dependency_remove", "dependency_update",
    "build_clean", "rename_symbol", "apply_patch",
    "task_delete",
})


def _agent_tool_mutates(tool_name, args):
    """True only when this invocation can change persistent workspace state."""
    args = args if isinstance(args, dict) else {}
    if tool_name not in _WORK_MUTATION_TOOLS:
        return False
    if tool_name == "file_delete":
        return args.get("dry_run") is False
    if tool_name == "json_patch":
        return str(args.get("mode", "preview")).strip().lower() == "apply"
    if tool_name == "text_patch":
        return args.get("apply") is True
    if tool_name == "rename_symbol":
        return args.get("dry_run") is False
    if tool_name == "data_convert":
        return args.get("apply") is True
    if tool_name == "sqlite_mutate":
        return str(args.get("mode", "preview")).strip().lower() == "apply"
    if tool_name in {
        "memory_quality_repair", "memory_privacy_repair",
        "memory_embedding_backfill", "memory_interaction_embedding_backfill",
    }:
        return args.get("apply") is True
    return True

# Read-only tools whose default (empty-args) invocation is meaningful, so a
# name-only branch prediction yields a deterministic call signature that can
# be speculatively executed and reliably matched against the real decision.
_SPECULATABLE_ARGFREE_TOOLS = frozenset({
    "workspace_inventory", "dependency_inventory", "directory_tree", "status", "activity_status",
    "context_health", "command_registry_list",
})
_WORK_VALIDATION_TOOLS = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "ground_artifact", "artifact_ground",
    "artifact_verify", "game_reference_suite", "game_generate_and_test",
    "game_generation_campaign", "self_heal_check", "workspace_inventory", "directory_tree", "file_find",
    "repository_symbol_index", "file_read", "file_read_range", "archive_extract", "archive_create", "text_search", "image_inspect",
    "memory_quality_report", "memory_privacy_review", "learning_health_status",
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
    if str(tool_name or "") == "archive_list":
        try:
            return bool(json.loads(str(observation or "")).get("valid"))
        except (TypeError, ValueError, json.JSONDecodeError):
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


def _agent_created_path_key(path):
    """One canonical key per on-disk target for the run-created-paths ledger.

    Case-folded and separator-normalized so "src\\a.h", "src/a.h" and
    "SRC/a.h" all name the same file on Windows. Not resolved against the
    CWD: the host-confined view from _project_scope_args is already the
    consistent form both the create and the retry present.
    """
    return os.path.normcase(os.path.normpath(str(path or "")))


def _agent_call_signature(tool_name, args):
    """Return a stable signature for equivalent host-scoped tool calls."""
    canonical = dict(args) if isinstance(args, dict) else args
    if isinstance(canonical, dict):
        if tool_name == "archive_create":
            root = os.path.realpath(os.path.normpath(str(canonical.get("root") or ".")))
            canonical["root"] = os.path.normcase(root)
            destination = str(canonical.get("destination") or "")
            if destination:
                if not os.path.isabs(destination):
                    destination = os.path.join(root, destination)
                canonical["destination"] = os.path.normcase(
                    os.path.realpath(os.path.normpath(destination))
                )
            try:
                inputs = archive_create_tool._parse_inputs(
                    canonical.get("inputs_json", canonical.get("inputs", []))
                )
                canonical["inputs_json"] = [
                    os.path.normcase(os.path.realpath(os.path.normpath(
                        value if os.path.isabs(value) else os.path.join(root, value)
                    )))
                    for value in inputs
                ]
                canonical.pop("inputs", None)
            except ValueError:
                pass
        path_keys = []
        if tool_name == "data_convert":
            path_keys.extend(("input_path", "output_path"))
        elif tool_name in {"file_copy", "file_move", "archive_extract"}:
            path_keys.extend(("source", "destination"))
        elif tool_name == "archive_create":
            path_keys = []
        elif tool_name in _PROJECT_SCOPED_PATH_TOOLS:
            path_keys.append(_project_scoped_path_key(tool_name))
        elif tool_name == "workspace_run":
            path_keys.append("cwd")
        elif tool_name == "script_run":
            path_keys.extend(("path", "cwd"))
        for key in path_keys:
            raw = canonical.get(key)
            if raw:
                try:
                    canonical[key] = os.path.normcase(
                        os.path.realpath(os.path.normpath(str(raw)))
                    )
                except (OSError, ValueError):
                    pass
    return (
        str(tool_name),
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str),
    )


def _agent_mutation_records(tool_name, args):
    args = args if isinstance(args, dict) else {}
    if tool_name == "file_batch_write":
        operations = _batch_agent_operations(args) or []
        return [
            {"tool": tool_name, "path": _agent_normalized_path(item.get("path", ""))}
            for item in operations if isinstance(item, dict)
        ]
    if tool_name == "text_patch":
        try:
            root = args.get("root") or "."
            return [
                {"tool": tool_name, "path": _agent_normalized_path(os.path.join(root, *item["path"].split("/")))}
                for item in text_patch_ops._parse(args.get("patch", ""))
            ]
        except (TypeError, ValueError, PermissionError):
            return [{"tool": tool_name, "path": _agent_normalized_path(args.get("root", ""))}]
    if tool_name == "data_convert":
        if args.get("apply") is not True:
            return []
        return [{
            "tool": tool_name,
            "path": _agent_normalized_path(args.get("output_path", "")),
        }]
    if tool_name == "archive_create":
        root = str(args.get("root") or ".")
        destination = str(args.get("destination") or "")
        if destination and not os.path.isabs(destination):
            destination = os.path.join(root, destination)
        return [{"tool": tool_name, "path": _agent_normalized_path(destination)}]
    path = args.get("path", "")
    if tool_name == "archive_extract":
        path = args.get("destination", "")
    elif tool_name == "artifact_generate":
        path = args.get("output_dir") or os.path.join(
            "artifacts", "generated", str(args.get("name", "generated-artifact")),
        )
    elif tool_name in {"game_generate_and_test", "game_generation_campaign"}:
        path = os.path.join("games", str(args.get("name", "generated-game")))
    elif tool_name in {"file_copy", "file_move"}:
        path = args.get("destination", "")
    record = {
        "tool": tool_name,
        "path": _agent_normalized_path(path),
    }
    if tool_name == "file_move":
        record["source"] = _agent_normalized_path(args.get("source", ""))
    return [record]


def _agent_mutation_record(tool_name, args):
    """Compatibility helper for callers that expect one mutation record."""
    records = _agent_mutation_records(tool_name, args)
    return records[0] if records else {"tool": tool_name, "path": ""}


def _agent_path_within(path, root):
    path = _agent_normalized_path(path)
    root = _agent_normalized_path(root)
    if not path or not root:
        return False
    try:
        return os.path.commonpath((path, root)) == root
    except (OSError, ValueError):
        return False


def _agent_argv(args):
    argv = args.get("args_json", args.get("args", []))
    if isinstance(argv, str):
        try:
            argv = json.loads(argv)
        except (TypeError, ValueError):
            argv = [argv]
    return [str(item) for item in (argv or [])]


def _agent_explicit_command_paths(argv, cwd):
    """Resolve path-looking argv entries against the validator working dir."""
    resolved = []
    for item in argv:
        text = str(item or "").strip()
        if not text or text.startswith("-"):
            continue
        looks_pathlike = (
            os.path.isabs(text)
            or bool(re.match(r"^[A-Za-z]:[\\/]", text))
            or "/" in text
            or "\\" in text
            or text in {".", ".."}
            or bool(os.path.splitext(text)[1])
        )
        if not looks_pathlike:
            continue
        candidate = text if os.path.isabs(text) else os.path.join(cwd, text)
        resolved.append(_agent_normalized_path(candidate))
    return [path for path in resolved if path]


def _agent_paths_covered_by_targets(paths, targets):
    return bool(paths and targets) and all(
        any(path == target or _agent_path_within(path, target) for target in targets)
        for path in paths
    )


def _agent_validation_covers(tool_name, args, mutations, observation=""):
    """Require validators to touch changed disk state, not equivalent draft code."""
    args = args if isinstance(args, dict) else {}
    records = [record for record in mutations if record.get("tool")]
    if not records:
        if tool_name in {"artifact_verify", "artifact_ground"}:
            return bool(str(args.get("path") or "").strip())
        if tool_name == "ground_artifact":
            checks = args.get("checks_json", args.get("checks", []))
            return bool(str(args.get("artifact") or "") and checks)
        if tool_name in {
            "game_reference_suite", "self_heal_check", "memory_quality_report",
            "memory_privacy_review", "learning_health_status",
        }:
            return True
    paths = [record["path"] for record in records if record.get("path")]
    target = _agent_normalized_path(args.get("path", args.get("artifact", "")))

    if tool_name == "archive_extract":
        destination = _agent_normalized_path(args.get("destination", ""))
        return bool(destination) and all(
            record["tool"] == "archive_extract"
            and record.get("path") == destination
            for record in records
        ) and '"validation_passed": true' in str(observation or "").lower()

    if tool_name == "archive_create":
        root = str(args.get("root") or ".")
        destination = str(args.get("destination") or "")
        if destination and not os.path.isabs(destination):
            destination = os.path.join(root, destination)
        destination = _agent_normalized_path(destination)
        return bool(destination) and all(
            record["tool"] == "archive_create"
            and record.get("path") == destination
            for record in records
        ) and '"ok": true' in str(observation or "").lower() and bool(
            re.search(r'"archive_sha256":\s*"[0-9a-f]{64}"', str(observation or "").lower())
        )

    if tool_name in {
        "game_reference_suite", "game_generate_and_test", "game_generation_campaign",
    }:
        game_path = _agent_normalized_path(
            os.path.join("games", str(args.get("name", "generated-game")))
        )
        return bool(records) and all(
            record["tool"] in {
                "game_generate_and_test", "game_generation_campaign",
            }
            and record.get("path") == game_path
            for record in records
        )
    if tool_name == "memory_quality_report":
        return bool(records) and all(
            record["tool"] == "memory_quality_repair" for record in records
        )
    if tool_name == "memory_privacy_review":
        return bool(records) and all(
            record["tool"] == "memory_privacy_repair" for record in records
        )
    if tool_name == "learning_health_status":
        return bool(records) and all(record["tool"] in {
            "memory_embedding_backfill",
            "memory_interaction_embedding_backfill",
        } for record in records)
    if tool_name in {"artifact_verify", "artifact_ground"}:
        return bool(records) and all(
            record["tool"] == "artifact_generate"
            and bool(target)
            and _agent_path_within(target, record.get("path", ""))
            for record in records
        )
    if tool_name == "script_run":
        if target and paths and all(path == target for path in paths):
            return True
        name = os.path.basename(target).lower()
        if not any(
            word in name for word in ("test", "check", "verify", "smoke", "build")
        ):
            return False
        cwd = _agent_normalized_path(
            args.get("cwd") or os.path.dirname(target)
        )
        if not paths:
            return bool(cwd)
        return bool(cwd) and all(
            _agent_path_within(path, cwd) for path in paths
        )
    if tool_name == "workspace_run":
        program = os.path.basename(str(args.get("program", ""))).lower()
        argv = _agent_argv(args)
        argv_text = [item.casefold() for item in argv]
        no_op_flags = {
            "--help", "-h", "--version", "--collect-only", "--co",
            "--list-tests", "--dry-run", "--fixtures", "--fixtures-per-test",
            "--show-only",
        }
        if any(item.split("=", 1)[0] in no_op_flags for item in argv_text):
            return False
        if program in {"ctest", "ctest.exe", "ninja", "ninja.exe"} and "-n" in argv_text:
            return False
        if "help" in argv_text and program in {
            "cmake", "cmake.exe", "ninja", "ninja.exe", "gradle", "gradle.bat",
            "mvn", "mvn.cmd", "npm", "npm.cmd", "cargo", "cargo.exe",
        }:
            return False
        clean_only = any(
            item == "clean"
            or item.endswith(":clean")
            or item in {"/t:clean", "-t:clean"}
            for item in argv_text
        )
        if clean_only:
            return False
        observation_lower = str(observation or "").casefold()
        if re.search(
            r"(?:no tests ran|collected\s+0\s+items|total tests:\s*0|"
            r"(?<!\d)0\s+tests\s+(?:passed|run)\b)",
            observation_lower,
        ):
            return False
        cwd = _agent_normalized_path(args.get("cwd") or ".")
        explicit_targets = _agent_explicit_command_paths(argv, cwd)
        explicit_coverage = _agent_paths_covered_by_targets(
            paths, explicit_targets,
        )
        if not paths:
            explicit_coverage = bool(explicit_targets)

        python_programs = {"python", "python.exe", "py", "py.exe"}
        node_programs = {"node", "node.exe"}
        if program in python_programs and any(
            flag in argv_text for flag in ("-c", "-command")
        ):
            return False
        if program in node_programs and any(
            flag in argv_text for flag in ("-e", "--eval", "-p", "--print")
        ):
            return False

        broad = program in {
            "pytest", "pytest.exe", "ctest", "ctest.exe", "ninja", "ninja.exe",
            "msbuild", "msbuild.exe",
        }
        if program in {"cmake", "cmake.exe"}:
            broad = "--build" in argv_text
        elif program in {"cargo", "cargo.exe"}:
            broad = any(action in argv_text for action in ("test", "check", "build"))
        elif program in {"dotnet", "dotnet.exe"}:
            broad = any(action in argv_text for action in ("test", "build"))
        elif program in {"npm", "npm.cmd"}:
            broad = (
                "test" in argv_text
                or (
                    "run" in argv_text
                    and any(action in argv_text for action in ("build", "check", "lint"))
                )
            )
        elif program in {"gradle", "gradle.bat", "mvn", "mvn.cmd"}:
            broad = any(
                action in argv_text
                for action in ("test", "check", "build", "verify", "package")
            )
        elif program in {"flutter", "flutter.bat", "dart", "dart.exe"}:
            broad = any(
                action in argv_text
                for action in ("test", "analyze", "build", "compile")
            )
        elif program in python_programs and "-m" in argv_text:
            module_index = argv_text.index("-m") + 1
            module = argv_text[module_index] if module_index < len(argv_text) else ""
            broad = module in {"pytest", "unittest"}
            if module in {"py_compile", "compileall"}:
                return explicit_coverage
        elif program in node_programs:
            broad = "--test" in argv_text

        if broad:
            return bool(cwd) and (
                not paths
                or all(_agent_path_within(path, cwd) for path in paths)
            )
        if program in (
            python_programs
            | node_programs
            | {"cl", "cl.exe", "g++", "g++.exe", "clang++", "clang++.exe"}
        ):
            return explicit_coverage
        return False
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
                    and record["tool"] in {
                        "directory_create", "file_copy", "file_move",
                    }
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
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "directory_digest", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "file_read_range", "context_pack",
    "text_search", "script_search", "program_search", "image_inspect", "repo_status", "repo_diff",
    "repo_log", "repo_show", "repo_blame",
    "data_inspect", "data_query", "project_detect", "archive_list", "artifact_risk_inspect",
    "verify_artifact",
    "memory_search", "learning_health_status", "evaluation_history_status",
    "memory_quality_report", "memory_privacy_review", "artifact_ground",
    "web_search", "web_fetch", "weather_lookup", "approximate_location_lookup",
    "status", "diagnostics", "process_list", "process_memory_risk_inspect",
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_audit", "find_references", "diff_files", "secret_scan",
    "build_run",
    "task_progress",
})
_AGENT_FILE_EVIDENCE_TOOLS = frozenset({
    "workspace_inventory", "workspace_compare", "directory_tree", "file_read", "file_read_range",
    "file_digest", "directory_digest", "file_find", "text_search",
    "script_search", "image_inspect", "log_inspect", "data_inspect", "data_query", "project_detect",
    "context_pack", "repo_log", "repo_show", "repo_blame", "archive_list",
    "dependency_inventory", "artifact_risk_inspect",
})
_AGENT_DEDUPLICATED_INSPECTION_TOOLS = frozenset({
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "directory_digest", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "file_read_range", "context_pack",
    "data_inspect", "data_query", "text_search", "script_search",
    "program_search", "image_inspect", "environment_status", "hardware_profile", "repo_status", "repo_diff", "project_detect",
    "repo_log", "repo_show", "repo_blame", "archive_list", "artifact_risk_inspect",
    "process_list", "process_memory_risk_inspect",
})
_AGENT_EXECUTION_STATE_INVALIDATION_TOOLS = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "workflow_run",
})
_LOCAL_AGENT_NUM_PREDICT = 1200
# A hosted agent decision may contain a complete bounded file_write payload.
# The per-call ceiling accommodates substantial native arguments, but exact
# 64 KiB payloads may still require chunks because characters are not tokens.
_CLOUD_AGENT_NUM_PREDICT = 16384
_CLOUD_AGENT_OUTPUT_BUDGET = 65536
_CLOUD_AGENT_WRITE_CHUNK_HINT = 24000


def _bounded_cloud_agent_generate(
    gen,
    *,
    per_call_limit=_CLOUD_AGENT_NUM_PREDICT,
    total_budget=_CLOUD_AGENT_OUTPUT_BUDGET,
    budget_state=None,
):
    """Hard-bound aggregate hosted output while preserving actual usage data."""
    per_call_limit = max(1, int(per_call_limit))
    total_budget = max(per_call_limit, int(total_budget))
    if budget_state is None:
        budget_state = {"spent": 0, "total": total_budget}
    else:
        budget_state.setdefault("spent", 0)
        budget_state.setdefault("total", total_budget)
        total_budget = max(1, int(budget_state["total"]))

    def bounded(prompt, history=None):
        spent = max(0, int(budget_state.get("spent", 0)))
        remaining = total_budget - spent
        if remaining <= 0:
            raise ModelCallError(
                "budget",
                "the bounded %d-token allowance for this agent run was consumed"
                % total_budget,
                attempts=0,
                cloud=True,
            )
        call_limit = min(per_call_limit, remaining)
        try:
            gen.num_predict_override = call_limit
        except (AttributeError, TypeError):
            pass
        try:
            content = gen(prompt, history=history)
        except ModelCallError as error:
            usage = dict(getattr(gen, "last_usage", None) or {})
            charged = _model_usage_count(usage.get("tokens_out"))
            # Empty/failed hosted responses may not expose usage to the
            # caller. Charge the full request ceiling so repeated failures
            # cannot evade the aggregate budget.
            if error.attempts <= 0:
                charged = 0
            else:
                charged = call_limit
            spent += charged
            budget_state["spent"] = spent
            bounded.last_usage = usage
            bounded.last_response_meta = dict(
                getattr(gen, "last_response_meta", None) or {}
            )
            bounded.output_tokens_used = spent
            raise
        finally:
            try:
                gen.num_predict_override = None
            except (AttributeError, TypeError):
                pass
        usage = dict(getattr(gen, "last_usage", None) or {})
        reported = _model_usage_count(usage.get("tokens_out"))
        estimated = max(1, _rough_token_count(content))
        # Provider usage metadata is external input. Never let a zero or
        # implausibly low count make nonempty/native-tool output free.
        charged = max(reported or 0, estimated)
        spent += charged
        budget_state["spent"] = spent
        bounded.last_usage = usage
        bounded.last_response_meta = dict(
            getattr(gen, "last_response_meta", None) or {}
        )
        bounded.output_tokens_used = spent
        return content

    bounded.last_usage = {}
    bounded.last_response_meta = {}
    bounded.output_tokens_used = 0
    bounded.output_token_budget = total_budget
    bounded.output_budget_state = budget_state
    return bounded


def _agent_project_scope(project):
    """Resolve a project directory while preserving bare checklist namespaces."""
    text = str(project or "").strip()
    if not text:
        return "", ""
    try:
        expanded = os.path.expanduser(text)
        candidate = os.path.realpath(os.path.abspath(expanded))
        if os.path.isdir(candidate):
            return candidate, ""
        path_like = (
            os.path.isabs(expanded)
            or bool(re.match(r"^[A-Za-z]:", expanded))
            or expanded.startswith((".", "~"))
            or "/" in expanded
            or "\\" in expanded
            or os.path.exists(candidate)
        )
        if path_like:
            return "", (
                "ERROR: invalid agent project root: path does not name an "
                "existing directory: %s" % candidate
            )
    except (OSError, ValueError) as exc:
        return "", "ERROR: invalid agent project root: %s" % exc
    return "", ""


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
    cancel_check=None,
) -> str:
    """Run a Claude-like local agent loop that can call tools.

    The model chooses one JSON tool call at a time, receives the observation,
    and continues until it returns {"final": "..."} or max_steps is reached.
    Tools include code execution, memory search, workflows, diagnostics, and
    public web search/fetch/weather when allow_web=True and web tools are on.
    """
    _maybe_live_reload()
    unsafe = unsafe_lab.active()
    if unsafe:
        # Unsafe lab mode is for a disposable host where the model is the
        # adversary under test. Remove all model-loop tool/root/read-only gates;
        # bounded execution time and the direct MCP tool implementations remain.
        allow_web = True
        allow_location = True
        read_only = False
        require_file_evidence = False
        project = ""
        required_tool_names = ()
        tool_allowlist = None
        tool_policy = None
        auto_checklist = False
    max_steps = _safe_limit(max_steps, 6, 20)
    model, cloud, augment, tier_label = _serve_target(tier, None)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: sonder, %s." % (tier, _valid_tier_names())
    if model is None:
        return "ERROR: `sonder:latest` Ollama alias not found."
    project_scope, project_error = _agent_project_scope(project)
    if project_error:
        if return_host_receipt:
            return autopilot_controller.HostTaskResult(
                output=project_error,
                project_scope="",
            )
        return project_error
    if cloud:
        default_agent_system = (
            "You are a hosted tool-using coding agent. Use only the tools listed "
            "in the task transcript; host policy may withhold private machine or "
            "workspace capabilities. Never invent tool results. Use web tools "
            "for current external information and cite fetched URLs in the final "
            "answer. Lead with the outcome and disclose failures."
        )
    else:
        default_agent_system = (
            "You are a local tool-using coding agent. Inspect real workspace evidence before making claims. "
            "For action tasks, use tools instead of merely describing commands. Prefer workspace_inventory, directory_tree, "
            "text_search, file_read_range, and program_search for discovery; use guarded file tools for "
            "mutations; validate every mutation with workspace_run, script_run, file_read_range, "
            "image_inspect, artifact_verify, or another path-specific checker before returning final. "
            "After editing a script, run that exact path "
            "with script_run; an equivalent run_code snippet does not validate the on-disk file. "
            "Never invent tool results. "
            "Use web tools for current external information and cite fetched URLs in the final answer. "
            "Your final answer must lead with the outcome, mention changed paths and checks, and disclose failures. "
            # One deterministic line about the host, so a local model picks the
            # right command shape instead of guessing. Never send this private
            # machine inventory to a hosted agent.
            + environment_probe.agent_brief()
        )
    # Hosted agents receive only the explicitly supplied/default hosted
    # system text. _build_system also appends mutable local profile, emotion,
    # goal, and runtime-identity blocks; those are useful local context but
    # are not part of the caller's cloud disclosure consent.
    system = (
        system or default_agent_system
        if cloud
        else _build_system(system or default_agent_system, False, "")
    )
    agent_num_predict = (
        _CLOUD_AGENT_NUM_PREDICT if cloud else _LOCAL_AGENT_NUM_PREDICT
    )
    cloud_budget_state = (
        {"spent": 0, "total": _CLOUD_AGENT_OUTPUT_BUDGET}
        if cloud else None
    )
    gen = _make_generate(
        model, system, 0.1, agent_num_predict, SESSION_NUM_CTX, cloud=cloud,
        cancel_check=cancel_check,
        accept_native_tool_calls=True,
        compact_cloud_reasoning=True,
    )
    if cloud:
        gen = _bounded_cloud_agent_generate(
            gen,
            per_call_limit=agent_num_predict,
            total_budget=_CLOUD_AGENT_OUTPUT_BUDGET,
            budget_state=cloud_budget_state,
        )
    observations = []
    file_evidence = False
    used_tool = False
    inspected = False
    mutated = False
    validation_attempted = False
    validation_ok = False
    mutations = []
    required_tools = frozenset(
        _canonical_agent_tool_name(name) for name in required_tool_names if name
    )
    allowed_tools = (
        None if tool_allowlist is None
        else frozenset(
            _canonical_agent_tool_name(name) for name in tool_allowlist if name
        )
    )
    used_tool_names = set()
    successful_web_calls = set()
    successful_inspection_results = {}
    repeated_inspection_counts = {}
    failed_call_counts = {}
    # Paths this run itself created via file_write mode=create. Re-creating one
    # of them is unambiguous intent to replace the run's own file, so the host
    # promotes the retry to mode=overwrite deterministically instead of letting
    # the model burn steps on the "file exists" error (measured: 5 of 12 steps
    # lost to that loop). Pre-existing files are never auto-overwritten.
    run_created_paths = set()
    claim_review_requests = 0
    # Branch prediction + speculative execution (advisory; a mispredict costs
    # at most one wasted read-only call and never touches durable state).
    _predictor = sonder_speculation.default_predictor()
    _spec_enabled = (
        sonder_speculation.speculation_enabled() and not cloud
    )

    def _spec_dispatch(tool_name, args):
        observation = _agent_dispatch_observed(
            tool_name, args, allow_web=False, read_only=True,
            project=project_scope,
        )
        return observation, _agent_tool_observation_ok(tool_name, observation)

    _spec_engine = sonder_speculation.SpeculationEngine(
        _predictor, _spec_dispatch, enabled=_spec_enabled,
    )
    # Argument-level prediction: a stream prefetcher over observed listings
    # lets the host speculate concrete file_read calls, not just arg-free
    # inspections (see sonder_speculation.FilePrefetcher).
    _spec_prefetcher = sonder_speculation.FilePrefetcher()
    _last_tool_name = None
    checklist_id, checklist_states = (
        _start_agent_checklist(prompt, project, read_only)
        if auto_checklist else ("", {})
    )
    # Filesystem scope: a real directory roots file and execution tools there.
    # A clear bare namespace label such as "default" remains checklist-only;
    # path-like typos were rejected above rather than failing open to Sonder's
    # own workspace.
    transcript = "Task:\n%s\n\n%s" % (
        prompt, _agent_tool_help(read_only=read_only, cloud=cloud)
    )
    if unsafe:
        transcript = "%s\n\n%s" % (unsafe_lab.WARNING, transcript)
    if project_scope:
        transcript += (
            "\n\nPROJECT ROOT: %s\nYour file/inspection tools are rooted at this "
            "directory: a relative path or '.' inspects the PROJECT, not Sonder's "
            "own workspace. Use paths relative to the project root." % project_scope
        )
    if allowed_tools is not None:
        transcript += (
            "\n\nHOST TOOL ALLOWLIST (cannot be expanded by the model):\n- %s"
            % "\n- ".join(sorted(allowed_tools))
        )

    def ensure_not_cancelled():
        if cancel_check is not None and _cancel_requested(cancel_check):
            raise ModelCallError(
                "cancelled",
                "agent call cancelled before another model/tool action",
                attempts=0,
                cloud=cloud,
            )

    def _teardown_speculation():
        # Squash anything in flight and persist the learned predictor so it
        # warms across runs. Never fatal to the agent result.
        try:
            _spec_engine.discard()
            _predictor.save()
        except Exception:
            pass

    def _attach_tool_evidence(text):
        """Append exactly one host-owned evidence section.

        Model text may quote or fabricate the public marker. Neutralize those
        occurrences before adding the host ledger so downstream validation can
        require one structural marker instead of trusting model-controlled
        prose.
        """
        text = str(text or "")
        marker = "=== TOOL EVIDENCE ==="
        if include_evidence and observations:
            text = text.replace(marker, "=== UNTRUSTED TOOL EVIDENCE MARKER ===")
            text += "\n\n%s\n%s" % (marker, "\n\n".join(observations))
        return text

    def finish_final(final):
        _teardown_speculation()
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
        final = _attach_tool_evidence(final)
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
                project_scope=project_scope,
            )
        return final

    def _early_exit(text: str):
        """Wrap a pre-finalization exit (parse failure, EVIDENCE_REQUIRED,
        max_steps abort) so a promised host receipt still carries the real
        project scope instead of silently defaulting to an empty one -- a bare
        string here previously read as ``actual=''`` in the caller's scope
        check, misreporting a normal evidence/parse failure as a scope
        mismatch.
        """
        _teardown_speculation()
        text = str(text or "")
        # Early exits are still auditable outcomes.  A worker may have already
        # collected valid, host-observed repository evidence before the model
        # exhausted its step budget, repeated a cached inspection, or failed
        # final synthesis.  Preserve that ledger exactly as finish_final does;
        # otherwise the orchestrator sees a receipt naming a real evidence tool
        # but no evidence section and incorrectly downgrades the whole lane to
        # EVIDENCE_REQUIRED.
        text = _attach_tool_evidence(text)
        if return_host_receipt:
            return autopilot_controller.HostTaskResult(
                output=text,
                tools=tuple(sorted(used_tool_names)),
                mutation_observed=mutated,
                validation_attempted=validation_attempted,
                validation_passed=validation_ok,
                project_scope=project_scope,
            )
        return text

    def run_claim_review_action(review, review_number):
        nonlocal file_evidence, inspected, used_tool
        tool_name = str(review.get("tool") or "")
        tool_args = review.get("args") or {}
        # Validate the same host-scoped arguments that dispatch will use.  A
        # repository model commonly echoes the absolute PROJECT ROOT from its
        # prompt; checking the raw model arguments first incorrectly rejected
        # that path even though the host had already authorized and confined
        # the run to ``project_scope``.
        policy_tool_args = _project_scope_args(
            tool_name, tool_args, project_scope,
        )
        policy_error = ""
        if tool_name not in _AGENT_CLAIM_REVIEW_TOOLS:
            policy_error = "ERROR: HOST CLAIM REVIEW: no approved evidence tool was supplied."
        elif allowed_tools is not None and tool_name not in allowed_tools:
            policy_error = (
                "ERROR: HOST CLAIM REVIEW: tool '%s' is outside this run's allowlist."
                % tool_name
            )
        if not policy_error and tool_policy is not None:
            policy_error = str(tool_policy(tool_name, policy_tool_args) or "")
        if not policy_error and cloud:
            policy_error = _cloud_agent_tool_policy_error(tool_name)
        if not policy_error:
            policy_error = _repository_read_only_error(
                tool_name,
                policy_tool_args,
                trusted_extra_roots=project_scope,
            )
        if policy_error:
            observation_text = policy_error
        else:
            ensure_not_cancelled()
            observation_text = str(_agent_dispatch_observed(
                tool_name,
                tool_args,
                allow_web=False,
                read_only=True,
                project=project_scope,
            ))
        tool_ok = _agent_tool_observation_ok(tool_name, observation_text)
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
        # Squash any speculation left unretired by the previous step (the
        # model went final, hit a cache, or committed to a different call).
        _spec_engine.discard()
        step_prompt = transcript
        if observations:
            step_prompt += "\n\n" + _agent_observation_prompt(observations)
        step_prompt += "\n\nChoose the next tool call or final answer."
        # Branch predict + speculatively execute a read-only call while the
        # model generates its decision. Only argument-free read-only tools
        # are speculated so the predicted call signature is deterministic;
        # the buffered result is retired only if the model commits to the
        # same call, otherwise it is squashed (see sonder_speculation).
        _spec_state = sonder_speculation.BranchPredictor.loop_state(
            tuple(used_tool_names), _last_tool_name, step,
        )
        _spec_prediction = _predictor.predict_next_tool(_spec_state)
        _spec_issued = False
        # Cost-model gate: only speculate when the expected hidden wall time
        # clears the floor on THIS machine (dormant on fast-tool CPUs, active
        # on slow-model/slow-tool hardware). See sonder_speculation.
        if (
            _spec_enabled
            and _spec_prediction is not None
            and _spec_prediction[0] in _SPECULATABLE_ARGFREE_TOOLS
            and _predictor.should_speculate(_spec_prediction[1])
        ):
            _predicted_tool = _spec_prediction[0]
            _predicted_args = _project_scope_args(
                _predicted_tool, {}, project_scope,
            )
            _spec_issued = _spec_engine.begin(
                _predicted_tool,
                _agent_call_signature(_predicted_tool, _predicted_args),
                _predicted_args,
            )
        if _spec_enabled and not _spec_issued and _predictor.should_speculate(0.5):
            # Fall back to the stream prefetcher: a concrete predicted
            # file_read from the last observed listing (argument-level
            # speculation, still strictly read-only).
            _prefetch_args = _spec_prefetcher.predict_read()
            if _prefetch_args is not None:
                _prefetch_scoped = _project_scope_args(
                    "file_read", _prefetch_args, project_scope,
                )
                _spec_engine.begin(
                    "file_read",
                    _agent_call_signature("file_read", _prefetch_scoped),
                    _prefetch_scoped,
                )
        _spec_decision_t0 = time.monotonic()
        decision, raw, decision_error = _agent_generate_decision(gen, step_prompt)
        _predictor.observe_decision_latency(time.monotonic() - _spec_decision_t0)
        if decision is None:
            if isinstance(decision_error, ModelCallError):
                if auto_checklist:
                    _agent_checklist_fail(
                        checklist_id, checklist_states,
                        "model request failed before a valid tool decision", 1,
                    )
                return _early_exit(_format_model_call_error(decision_error))
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model returned an invalid tool decision", 1,
                )
            return _early_exit(
                "ERROR: could not parse agent decision at step %d: %s\nraw=%s" % (
                    step, decision_error, raw[:1000])
            )
        if "final" in decision:
            final = str(decision.get("final") or "")
            if required_tools and not (required_tools & used_tool_names):
                if step < max_steps:
                    observations.append(
                        "HOST REQUIREMENT: use at least one successful tool from: %s."
                        % ", ".join(sorted(required_tools))
                    )
                    continue
                return _early_exit(
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
            if not unsafe and _AGENT_NEGATIVE_CLAIM_RE.search(final):
                claim_review = _agent_negative_claim_review(
                    prompt, final, observations, model, cloud=cloud,
                    cancel_check=cancel_check,
                    cloud_budget_state=cloud_budget_state,
                )
                if claim_review["decision"] == "error":
                    if auto_checklist:
                        _agent_checklist_fail(
                            checklist_id, checklist_states,
                            "negative-claim reviewer model request failed", 1,
                        )
                    return _early_exit(claim_review["reason"])
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
                    return _early_exit("%s: %s\n\n%s" % (
                        master_orchestrator.EVIDENCE_REQUIRED,
                        claim_review["reason"],
                        "\n\n".join(observations),
                    ))
            if require_file_evidence and not file_evidence:
                if auto_checklist:
                    _agent_checklist_fail(
                        checklist_id, checklist_states,
                        "required workspace evidence was not collected", 1,
                    )
                detail = "\n\n" + "\n\n".join(observations) if observations else ""
                return _early_exit(master_orchestrator.EVIDENCE_REQUIRED + detail)
            return finish_final(final)
        tool_name = _canonical_agent_tool_name(decision.get("tool"))
        if not tool_name:
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model decision omitted both tool and final", 1,
                )
            return _early_exit(
                "ERROR: agent decision missing 'tool' or 'final': %s" % decision
            )
        tool_args = decision.get("args", {})
        if not isinstance(tool_args, dict):
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model tool arguments were not a JSON object", 1,
                )
            return _early_exit(
                "ERROR: agent tool arguments must be a JSON object"
            )
        # Learn the branch: predictor conditions the next-tool table on the
        # loop state that preceded this committed decision, and prediction
        # accuracy is scored here at commit time — independent of whether a
        # speculation was issued for it.
        if _spec_prediction is not None:
            if _spec_prediction[0] == tool_name:
                _predictor.note_hit()
            else:
                _predictor.note_miss()
        _predictor.record_transition(_spec_state, tool_name)
        _last_tool_name = tool_name
        # Keep policy and dispatch on one canonical, host-confined view of a
        # repository tool call.  Previously the early read-only check saw raw
        # model paths while dispatch later rebased them under ``project_scope``.
        # Absolute in-project paths were therefore rejected before dispatch,
        # causing fleet workers to exhaust max_steps without any file evidence.
        policy_tool_args = _project_scope_args(
            tool_name, tool_args, project_scope,
        )
        # HOST AUTO-PROMOTE: a mode=create write to a path this run already
        # created is unambiguous intent to replace the run's own file. Promote
        # it to overwrite deterministically -- the model otherwise loops on
        # "file exists" errors until the no-progress guard kills the run.
        auto_promoted_overwrite = False
        if (
            tool_name == "file_write"
            and str(policy_tool_args.get("mode") or "create").lower() == "create"
            and _agent_created_path_key(policy_tool_args.get("path"))
            in run_created_paths
        ):
            policy_tool_args = dict(policy_tool_args)
            policy_tool_args["mode"] = "overwrite"
            auto_promoted_overwrite = True
        call_signature = _agent_call_signature(tool_name, policy_tool_args)
        cached_inspection = (
            tool_name in _AGENT_DEDUPLICATED_INSPECTION_TOOLS
            and call_signature in successful_inspection_results
        )
        prior_identical_failures = failed_call_counts.get(call_signature, 0)
        if prior_identical_failures >= 3:
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model repeated an unchanged failing tool call", 2,
                )
            return _early_exit(
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
            policy_error = str(tool_policy(tool_name, policy_tool_args) or "")
        if not policy_error and cloud:
            policy_error = _cloud_agent_tool_policy_error(
                tool_name, unsafe=unsafe,
            )
        if not policy_error and project_scope:
            policy_error = _repository_scope_path_error(
                tool_name, policy_tool_args, project_scope,
            )
        if not policy_error and project_scope and tool_name not in _PROJECT_BOUND_AGENT_TOOLS:
            policy_error = (
                "ERROR: HOST POLICY: tool '%s' has no project-bound execution "
                "contract and is disabled for a project-bound agent."
                % tool_name
            )
        if not policy_error and project_scope:
            policy_error = _agent_project_execution_argument_error(
                tool_name, policy_tool_args, project_scope,
            )
        if not policy_error and read_only:
            policy_error = _repository_read_only_error(
                tool_name,
                policy_tool_args,
                trusted_extra_roots=project_scope,
            )
        if (
            auto_checklist
            and _agent_tool_mutates(tool_name, policy_tool_args)
            and not inspected
            and not policy_error
        ):
            policy_error = (
                "ERROR: HOST REQUIREMENT: inspect relevant workspace evidence "
                "before making a mutation."
            )
        tool_dispatched = False
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
        elif cached_inspection:
            repeated = repeated_inspection_counts.get(call_signature, 0) + 1
            repeated_inspection_counts[call_signature] = repeated
            if repeated >= 3:
                if auto_checklist:
                    _agent_checklist_fail(
                        checklist_id, checklist_states,
                        "model repeated an unchanged successful inspection", 2,
                    )
                return _early_exit(
                    "ERROR: agent repeated the same already-successful inspection "
                    "%d times: %s. Use the existing evidence, change the arguments, "
                    "or make a relevant state change.\n\n%s"
                    % (repeated, tool_name, "\n\n".join(observations))
                )
            observation = (
                "HOST CACHED INSPECTION: this identical call already succeeded; "
                "reusing its prior observation without dispatching it again. "
                "Read the cached hit below and either finalize from it now or "
                "change the arguments to inspect different evidence; do not repeat "
                "this identical call.\n"
                + successful_inspection_results[call_signature]
            )
        elif cloud and tool_name == "tool_manifest":
            # The direct/local manifest remains authoritative and complete,
            # while a hosted model sees only the capabilities it may request.
            observation = _agent_tool_help(read_only=read_only, cloud=True)
        elif read_only and tool_name in {"command_registry_list", "tool_manifest"}:
            observation = _agent_tool_help(read_only=True)
        else:
            ensure_not_cancelled()
            # Retire a matching speculation: if the model committed to the
            # exact read-only call the host already ran during generation,
            # reuse its buffered observation instead of dispatching again.
            _retired = _spec_engine.resolve(call_signature)
            if _retired is not None:
                tool_dispatched = True
                observation = _retired.observation
                # Feed the cost model: the tool's measured wall time, and the
                # portion of it hidden behind this step's model decision.
                _tool_wall = _retired.wall_seconds
                _predictor.observe_tool_latency(_tool_wall)
                _predictor.note_saved(
                    min(_tool_wall, time.monotonic() - _spec_decision_t0)
                )
            else:
                dispatch_options = {
                    "allow_web": allow_web,
                    "read_only": read_only,
                }
                if allow_location:
                    dispatch_options["allow_location"] = True
                tool_dispatched = True
                observation = _agent_dispatch_observed(
                    tool_name, policy_tool_args, project=project_scope,
                    **dispatch_options,
                )
        observation_text = str(observation)
        tool_ok = _agent_tool_observation_ok(tool_name, observation)
        if tool_ok:
            failed_call_counts.pop(call_signature, None)
            if tool_name == "file_write":
                run_created_paths.add(
                    _agent_created_path_key(policy_tool_args.get("path"))
                )
                if auto_promoted_overwrite:
                    observation_text += (
                        "\nHOST AUTO-PROMOTE: mode=create was replaced with "
                        "mode=overwrite because this run created the file."
                    )
            elif tool_name == "file_batch_write":
                for item in _batch_agent_operations(policy_tool_args) or []:
                    if (
                        isinstance(item, dict)
                        and str(item.get("mode") or "").lower() == "create"
                    ):
                        run_created_paths.add(
                            _agent_created_path_key(item.get("path"))
                        )
            elif tool_name == "data_convert" and policy_tool_args.get("apply") is True:
                run_created_paths.add(
                    _agent_created_path_key(policy_tool_args.get("output_path"))
                )
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
            elif tool_name == "file_write" and "file exists" in observation_text.lower():
                recovery += (
                    " To replace the existing file, repeat the call with "
                    "mode=overwrite."
                )
            elif tool_name == "file_edit" and "old text must not be empty" in observation_text.lower():
                recovery += (
                    " file_edit replaces a non-empty old text; read the file "
                    "first, or use file_write with mode=overwrite to rewrite it."
                )
            observation_text += "\n" + recovery
        used_tool = used_tool or tool_ok
        # Train the stream prefetcher on what the model actually did (raw
        # args: listing entries and model read paths share the same
        # scope-relative form).
        _spec_prefetcher.observe(
            tool_name, tool_args, observation_text, ok=tool_ok,
        )
        if tool_ok:
            used_tool_names.add(str(tool_name))
            if tool_name in {
                "web_search", "web_fetch", "weather_lookup",
                "approximate_location_lookup",
            }:
                successful_web_calls.add(call_signature)
            if (
                tool_name in _AGENT_DEDUPLICATED_INSPECTION_TOOLS
                and not cached_inspection
            ):
                successful_inspection_results[call_signature] = observation_text[:6000]
                repeated_inspection_counts.pop(call_signature, None)
        if tool_name in _AGENT_FILE_EVIDENCE_TOOLS and tool_ok:
            file_evidence = True
        if auto_checklist and tool_name in _WORK_INSPECTION_TOOLS and tool_ok:
            inspected = True
            _agent_checklist_mark(
                checklist_id, checklist_states, 1, "done", "%s completed" % tool_name,
            )
            _agent_checklist_mark(
                checklist_id, checklist_states, 2, "in_progress", "working from inspected evidence",
            )
        mutation_happened = _agent_tool_mutates(
            tool_name, policy_tool_args,
        ) and tool_ok
        mutation_attempt_may_have_changed = (
            tool_dispatched
            and _agent_tool_mutates(tool_name, policy_tool_args)
        )
        if mutation_attempt_may_have_changed:
            # A failed mutator can still leave directories or partial output.
            # Treat the workspace as dirty until a grounded validator proves
            # otherwise instead of reporting mutation_observed=False.
            mutated = True
        execution_may_have_changed = (
            tool_dispatched
            and tool_name in _AGENT_EXECUTION_STATE_INVALIDATION_TOOLS
        )
        if mutation_attempt_may_have_changed or execution_may_have_changed:
            # A real mutation or an execution-capable tool can make prior
            # inspection results stale even when the command exits nonzero.
            # Dry-run mutation tools do not reach here.
            successful_inspection_results.clear()
            repeated_inspection_counts.clear()
            validation_attempted = False
            validation_ok = False
            if mutation_happened or tool_ok:
                failed_call_counts.clear()
            else:
                # The failing execution itself must remain bounded, while
                # failures against the pre-execution workspace may be stale.
                current_failure_count = failed_call_counts.get(call_signature, 0)
                failed_call_counts.clear()
                if current_failure_count:
                    failed_call_counts[call_signature] = current_failure_count
        if mutation_attempt_may_have_changed:
            for record in _agent_mutation_records(tool_name, policy_tool_args):
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
                tool_name, policy_tool_args, mutations, observation_text,
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
            if isinstance(final_error, ModelCallError):
                if auto_checklist:
                    active_item = 3 if validation_attempted else 2 if mutated else 1
                    _agent_checklist_fail(
                        checklist_id, checklist_states,
                        "model request failed during final synthesis",
                        active_item,
                    )
                return _early_exit(_format_model_call_error(final_error))
            if auto_checklist:
                active_item = 3 if validation_attempted else 2 if mutated else 1
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "agent could not synthesize a final answer after max_steps",
                    active_item,
                )
            return _early_exit(
                "ERROR: agent reached max_steps=%d and finalization failed: %s\n"
                "raw=%s\n\n%s"
                % (max_steps, final_error, raw[:1000], "\n\n".join(observations))
            )
        final = str(final_decision.get("final") or "")
        if not _AGENT_NEGATIVE_CLAIM_RE.search(final):
            break
        claim_review = _agent_negative_claim_review(
            prompt, final, observations, model, cloud=cloud,
            cancel_check=cancel_check,
            cloud_budget_state=cloud_budget_state,
        )
        if claim_review["decision"] == "error":
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "negative-claim reviewer model request failed at finalization",
                    1,
                )
            return _early_exit(claim_review["reason"])
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
        return _early_exit("%s: %s\n\n%s" % (
            master_orchestrator.EVIDENCE_REQUIRED,
            claim_review["reason"],
            "\n\n".join(observations),
        ))
    if required_tools and not (required_tools & used_tool_names):
        return _early_exit(
            "ERROR: agent reached max_steps=%d without using a required web tool (%s)."
            % (max_steps, ", ".join(sorted(required_tools)))
        )
    if auto_checklist and not used_tool:
        _agent_checklist_fail(
            checklist_id, checklist_states,
            "agent exhausted tool steps without successful evidence", 1,
        )
        return _early_exit(
            "ERROR: agent reached max_steps=%d without successful tool evidence." % max_steps
        )
    if require_file_evidence and not file_evidence:
        if auto_checklist:
            _agent_checklist_fail(
                checklist_id, checklist_states,
                "required workspace evidence was not collected", 1,
            )
        detail = "\n\n" + "\n\n".join(observations) if observations else ""
        return _early_exit(master_orchestrator.EVIDENCE_REQUIRED + detail)
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
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "directory_digest", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "file_read_range", "data_inspect", "data_query", "text_search", "script_search",
    "project_detect",
    "repo_status", "repo_diff", "repo_log", "repo_show", "repo_blame", "archive_list", "artifact_risk_inspect",
    "program_search", "image_inspect", "memory_search", "process_list", "process_memory_risk_inspect", "web_search",
    "web_fetch", "weather_lookup", "status", "diagnostics",
    "context_health", "learning_health_status", "memory_quality_report", "system_improvement_report", "artifact_ground",
    "test_discover", "find_references", "diff_files", "secret_scan",
    "dependency_audit",
    "task_progress",
})
_AUTOPILOT_WORKSPACE_TOOLS = _AUTOPILOT_OBSERVE_TOOLS | frozenset({
    "directory_create", "file_write", "file_batch_write", "json_patch", "file_edit", "file_copy", "file_move", "archive_extract", "archive_create", "text_patch", "data_convert", "workspace_run",
    "script_run", "run_code", "run_project", "ground_artifact", "artifact_ground",
    "artifact_generate", "artifact_verify", "game_reference_suite",
    "game_generate_and_test",
    "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag", "git_merge", "git_cherry_pick",
    "build_run", "build_clean", "rename_symbol", "apply_patch",
    "task_delete", "task_plan", "task_depend",
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
    "directory_create", "file_write", "file_batch_write", "json_patch", "file_edit", "file_copy", "file_move", "archive_extract", "archive_create", "text_patch", "data_convert", "artifact_generate",
    "game_generate_and_test",
})


def _autopilot_allowed_tools(run: dict) -> frozenset | None:
    if unsafe_lab.active():
        return None
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
    if unsafe_lab.active():
        return None
    project_scope, _project_error = _agent_project_scope(run.get("project", ""))
    allowed_tools = _autopilot_allowed_tools(run)

    def check(tool_name, args):
        args = args if isinstance(args, dict) else {}
        if tool_name not in allowed_tools:
            return "ERROR: HOST POLICY: tool '%s' is not allowed for this autonomous run." % tool_name
        if tool_name in _GIT_IGNORE_DISCOVERY_TOOLS and args.get("include_ignored"):
            return (
                "ERROR: HOST POLICY: autonomous runs cannot set "
                "include_ignored=true."
            )
        host_scoped_text_patch = (
            tool_name == "text_patch"
            and bool(project_scope)
            and not args.get("token")
            and args.get("approval") is _TRUSTED_REPOSITORY_APPROVAL
            and args.get("extra_roots") == project_scope
        )
        if (
            any(args.get(name) for name in ("token", "approval", "extra_roots"))
            and not host_scoped_text_patch
        ):
            return "ERROR: HOST POLICY: autonomous runs cannot use bypass credentials or extra roots."
        if tool_name in {"file_copy", "file_move"}:
            if not str(args.get("source") or "").strip() or not str(
                args.get("destination") or ""
            ).strip():
                return "ERROR: HOST POLICY: autonomous file transfers require exact source and destination paths."
            if "overwrite" in args and args.get("overwrite") is not False:
                return (
                    "ERROR: HOST POLICY: autonomous file transfers require "
                    "overwrite to be the boolean false."
                )
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
    allowed_set = _autopilot_allowed_tools(run)
    allowed = (
        sorted(allowed_set)
        if allowed_set is not None
        else ["UNRESTRICTED HOST-NATIVE AGENT TOOLS (UNSAFE LAB MODE)"]
    )
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
        # Truncate an over-long initial plan rather than failing the whole
        # autonomous run over it. The prompt asks the model to keep the initial
        # plan within initial_limit "so adaptive review has room to replace
        # stale pending work" -- truncation IS that intent. A local 7B planner
        # routinely emits one or two tasks too many; previously that raised, the
        # single retry often over-planned again, and the entire run failed
        # ("initial plan exceeds the 3-task adaptive planning limit") before a
        # single task ran. Trim the surplus (the caller re-normalizes and
        # re-appends the grounded validate task) so the run proceeds; adaptive
        # review adds tasks back if the objective needs them.
        tasks = payload.get("tasks")
        if isinstance(tasks, list) and len(tasks) > initial_limit:
            payload["tasks"] = tasks[:initial_limit]
        normalized = autopilot_controller.normalize_plan(
            payload, run.get("objective", ""), max_tasks,
        )
        if not unsafe_lab.active() and run.get("policy") == "observe" and any(
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
        # Sanitize benign local-model noise instead of failing the whole run.
        # A 7B reviewer routinely assesses already-completed tasks (unknown
        # pending id), repeats a task, or emits a junk verdict -- each of which
        # previously raised, survived a fruitless retry, and killed the run
        # mid-execution with valid pending tasks still queued. Drop the
        # unusable entries: the controller only ever acts on a "stale" verdict
        # for a genuinely-pending task, so discarding non-pending / malformed /
        # duplicate assessments changes no real decision. Any pending task the
        # reviewer left unassessed still defaults to "keep" below.
        assessed = {}
        reasons = {}
        for item in assessments:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "").strip()
            verdict = str(item.get("verdict") or "").strip().lower()
            if task_id not in pending_ids or verdict not in {"keep", "stale"}:
                continue
            assessed[task_id] = verdict  # a later duplicate simply overrides
            reasons[task_id] = str(item.get("reason") or "adaptive review").strip()
        rebuilt = [
            {"id": task_id, "verdict": verdict, "reason": reasons.get(task_id, "adaptive review")}
            for task_id, verdict in assessed.items()
        ]
        for task_id in sorted(pending_ids - set(assessed)):
            rebuilt.append({
                "id": task_id,
                "verdict": "keep",
                "reason": "host default: reviewer did not mark this pending task stale",
            })
            assessed[task_id] = "keep"
        payload["pending_assessment"] = rebuilt
        stale = {task_id for task_id, verdict in assessed.items() if verdict == "stale"}
        if stale and normalized["decision"] == "continue":
            raise ValueError("continue is invalid while a pending task is stale")
        if normalized["decision"] == "replan" and not stale:
            # "replan" with nothing marked stale is not actionable -- there is
            # nothing to replace -- so it means the same thing as "continue with
            # the existing pending plan". Coerce it rather than raising: a local
            # 7B reviewer routinely says "replan" while marking every pending
            # task keep, and failing the whole autonomous run over that
            # inconsistency (it previously raised, the retry repeated it, and the
            # run died mid-execution with valid pending tasks still queued) is far
            # worse than just continuing. The controller already treats a
            # replan-with-no-tasks/no-stale as continue by falling through; this
            # keeps the returned payload consistent with that.
            payload["decision"] = "continue"

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
    unsafe = unsafe_lab.active()
    output = _agent_impl(
        prompt,
        tier=run.get("tier", "code"),
        max_steps=12,
        allow_web=bool(run.get("allow_web")),
        require_file_evidence=False,
        read_only=(run.get("policy") == "observe" and not unsafe),
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
    run = _application().automation.get_run(run_id) or {}
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
    run = _application().automation.get_run(run_id)
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
        return autopilot_controller.format_run(_application().automation.get_run(run_id))
    return autopilot_controller.format_snapshot(
        autopilot_controller.snapshot(include_finished=include_finished),
    )


def _execution_route_model(
    prompt: str, project: str = "", tier_override: str = "",
) -> dict:
    """Let a local model choose only foreground workbench or Autopilot.

    ``tier_override`` lets offline tooling (the routing distiller) label with
    a stronger local judge than the production router tier; live routing
    never passes it.
    """
    router_tier = tier_override or runtime_policy.route_tier(
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
            # The mode router only ever offers the base tiers (see the prompt
            # above); specialist tiers are chosen afterwards by the capability
            # router, from the tiers the policy actually binds.
            if selected_tier not in runtime_policy.BASE_LOCAL_TIERS:
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


def _capability_refined_tier(
    prompt: str, selected_tier: str, reason: str, *, has_image: bool = False,
):
    """Upgrade a lane-selected tier to a configured specialist tier.

    Capability-aware tier refinement (SPEC-3 domain/routing). The execution
    mode decision is untouched, and the operator's lane -> tier mapping still
    wins for ordinary work: this only *upgrades* to a specialist tier the
    operator has actually bound (e.g. a reasoning model for a proof, a vision
    model for an image task). With only the base tiers bound it is a deliberate
    no-op. Advisory and fail-safe -- any error keeps the lane-selected tier, and
    it can only pick an already-configured local tier (never cloud, never a new
    permission).

    The vision tier additionally requires a real image signal. Keyword-only
    vision guesses are common in ordinary text work ("summarize what this chart
    shows"), and a vision-language model answers a text-only prompt with an
    immediate end-of-sequence -- so a keyword false positive would hand the
    whole run to a model that returns nothing.

    Returns ``(tier, reason)``.
    """
    try:
        from sonder_runtime.domain.routing import capability_router as _caprouter

        available = _configured_local_tiers() or runtime_policy.BASE_LOCAL_TIERS
        specialists = set(runtime_policy.OPTIONAL_LOCAL_TIERS) & set(available)
        if specialists:
            route = _caprouter.route(prompt, available, has_image=has_image)
            if route.tier == "vision" and not has_image:
                return selected_tier, reason
            if route.tier in specialists and route.tier != selected_tier:
                return route.tier, "%s; capability route: %s" % (reason, route.task)
    except Exception:
        pass
    return selected_tier, reason


def route_work_request(prompt: str, project: str = "") -> str | None:
    """Transparently route eligible natural work to a bounded execution lane."""
    _maybe_live_reload()
    explicit_worker_cap = master_orchestrator.requested_worker_cap(prompt)
    decision = (
        {
            "mode": "fleet",
            "reason": "explicit bounded worker-count request",
            "plan_only": False,
            "actions": [],
        }
        if explicit_worker_cap else intents.classify_execution(prompt)
    )
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
        # The NPU utility accelerator may pre-score only this ambiguous band.
        # Deterministic host cues never reach it, its output is allowlist-
        # validated inside npu_service, and any miss lands on the existing
        # local router — an accelerator problem can never widen behavior.
        npu_decision = None
        npu_prefer_active = False
        with contextlib.suppress(Exception):
            npu_prefer_active = npu_service.routing_active() == "prefer"
        try:
            npu_decision = npu_service.route_decide(prompt)
        except Exception:
            npu_decision = None
        validated_npu = None
        if isinstance(npu_decision, dict):
            candidate_mode = str(
                npu_decision.get("mode") or ""
            ).strip().lower()
            candidate_tier = str(
                npu_decision.get("tier") or ""
            ).strip().lower()
            candidate_reason = npu_decision.get("reason")
            candidate_confidence = npu_decision.get("confidence")
            if (
                candidate_mode in npu_contract.ROUTE_MODES
                # The accelerator pre-scores the same lane decision the mode
                # router makes, so it is held to the same base-tier allowlist.
                and candidate_tier in runtime_policy.BASE_LOCAL_TIERS
                and isinstance(candidate_reason, str)
                and bool(candidate_reason.strip())
                and not isinstance(candidate_confidence, bool)
                and isinstance(candidate_confidence, (int, float))
                and 0.0 <= float(candidate_confidence) <= 1.0
            ):
                validated_npu = {
                    "mode": candidate_mode,
                    "reason": candidate_reason.strip()[:240],
                    "confidence": float(candidate_confidence),
                }
        npu_fallback_attempted = npu_prefer_active and not validated_npu
        if validated_npu:
            mode = validated_npu["mode"]
            selected_tier = runtime_policy.route_tier(
                mode, _RUNTIME_POLICY, fallback="code",
            )
            reason = validated_npu["reason"]
            confidence = validated_npu["confidence"]
            source = "npu accelerator"
        else:
            try:
                routed = _execution_route_model(prompt, project=project)
                mode = routed["mode"]
                selected_tier = routed.get("tier") or runtime_policy.route_tier(
                    mode, _RUNTIME_POLICY, fallback="code",
                )
                reason = routed["reason"]
                confidence = routed["confidence"]
                source = "bounded local mode model"
                if npu_fallback_attempted:
                    with contextlib.suppress(Exception):
                        npu_service.record_fallback_handler(
                            "routing", "ollama", True,
                        )
            except (OSError, RuntimeError, ValueError) as exc:
                if npu_fallback_attempted:
                    with contextlib.suppress(Exception):
                        npu_service.record_fallback_handler(
                            "routing", "host", True,
                        )
                mode = "autopilot"
                selected_tier = runtime_policy.route_tier(
                    "autopilot", _RUNTIME_POLICY, fallback="code",
                )
                reason = (
                    "compound-work fallback after local mode selection was unavailable: %s"
                    % re.sub(r"\s+", " ", str(exc))[:240]
                )
                source = "host fallback"
            with contextlib.suppress(Exception):
                npu_service.route_shadow(
                    prompt, {
                        "mode": mode,
                        "tier": selected_tier,
                        "handler": (
                            "ollama"
                            if source == "bounded local mode model"
                            else "host"
                        ),
                    },
                )

    selected_tier, reason = _capability_refined_tier(prompt, selected_tier, reason)

    resolved_project = _resolve_project(project) or ""
    if mode == "fleet":
        master_kwargs = {
            "task": prompt, "mode": "fleet", "tier": selected_tier,
            "learn": False,
        }
        if explicit_worker_cap:
            master_kwargs["agents"] = explicit_worker_cap
            master_kwargs["worker_cap"] = explicit_worker_cap
        if isinstance(resolved_project, str) and os.path.isdir(resolved_project):
            master_kwargs["project"] = resolved_project
        output = master_orchestrate(**master_kwargs)
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
            snapshot = _application().automation.snapshot(include_finished=False, limit=20)
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
            # An optional tier left unset has no model, so it cannot be missing.
            if str(model or "").strip()
            and not _runtime_model_is_installed(model, installed)
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


def npu_fallback_status_data() -> dict:
    """Enum-only NPU fallback state suitable for summary-mode status APIs."""
    try:
        return npu_service.fallback_projection()
    except Exception:
        return {
            "schema_version": 1,
            "known": False,
            "capabilities": {
                capability: {
                    "policy_mode": "unknown",
                    "role": "unknown",
                    "local_fallback_handler": "unknown",
                }
                for capability in ("routing", "embeddings")
            },
            "last_fallback": {
                "capability": "unknown",
                "reason": "unknown",
                "operation_mode": "unknown",
                "fallback_handler": "unknown",
                "handler_state": "unknown",
                "count": 0,
            },
            "reason_counts": {},
        }


@mcp.tool()
def npu_status(probe: bool = False) -> str:
    """Show the NPU utility accelerator: detected vs runtime-ready vs enabled
    vs healthy, provider capability, model bundle hashes, latency, fallback
    counters, and circuit state.

    The accelerator sits below every local model tier and is never
    a model tier itself. probe=True additionally triggers a non-blocking worker
    warmup when the runtime policy enables the accelerator.
    """
    _maybe_live_reload()
    try:
        state = npu_service.status(probe=probe is True)
        return npu_service.format_status(state)
    except Exception:
        return (
            "sonder npu accelerator\n"
            "  state: unknown (status unavailable)\n"
            "  boundary: NPU failure falls back to existing local behavior; "
            "cloud is never a fallback"
        )


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
    npu_json: str = "",
    reset: bool = False,
) -> str:
    """Guarded-edit shared local mappings; cloud configuration is never accepted.

    npu_json may set only the accelerator behavior modes, e.g.
    {"mode": "shadow", "routing": "prefer"} with modes off|shadow|prefer.
    """
    _maybe_live_reload()
    try:
        local_models = _runtime_update_object(local_models_json, "local_models_json")
        routing = _runtime_update_object(routing_json, "routing_json")
        npu = _runtime_update_object(npu_json, "npu_json")
        if local_models:
            models_to_validate = {
                tier: model for tier, model in local_models.items()
                if not (
                    tier in runtime_policy.OPTIONAL_LOCAL_TIERS
                    and not str(model or "").strip()
                )
            }
            if models_to_validate:
                installed = _runtime_installed_models()
                missing = [
                    str(model) for model in models_to_validate.values()
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
            npu=npu,
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
    apply = apply is True
    issues, actions = self_heal.repair(
        _DB_PATH,
        module_names=LIVE_RELOAD_MODULES,
        apply=apply,
    )
    return self_heal.format_report(issues, actions=actions)


@mcp.tool()
def diagnostics() -> str:
    """Run lightweight health checks for the local Sonder Runtime installation."""
    _maybe_live_reload()
    lines = ["sonder diagnostics"]
    lines.append("  unsafe lab mode: %s" % unsafe_lab.status_line())
    lines.append("  live reload: %s" % ("on" if live_reload.enabled() else "off"))
    lines.append(
        "  ollama endpoint: %s (%s; remote opt-in %s)"
        % (
            _ollama_display(),
            ollama_endpoint.locality(BASE),
            "on" if ollama_endpoint.remote_allowed() else "off",
        )
    )
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
    try:
        lines.append("  tool capability shadow: %s" % tool_capability_shadow_report())
    except Exception as e:
        lines.append("  tool capability shadow: ERROR validator failed: %s" % e)
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
    lines.append(
        "  loopback model retry: %d transient retry(s), %dms base delay; "
        "remote/cloud retries off"
        % (
            _local_model_retries(),
            int(_local_retry_delay(1) * 1000),
        )
    )
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
        # Never the blended rate alone. It is dominated by the runtime marking
        # its own curriculum, so on this store it reads 96% beside a "watch"
        # status -- which parses as "healthy, minor hygiene" when caller-judged
        # work is at 53%. Third consumer of this report; the other two were
        # fixed first and this one was missed.
        lines.append(
            "  learning health: %s (%s%% outcome coverage, "
            "caller-judged %s%% of %s, autograded %s%% of %s, yield=%s)"
            % (
                health["status"],
                health["outcome_coverage_percent"],
                health.get("reviewed_positive_percent", 0),
                health.get("reviewed_outcomes", 0),
                health.get("autograded_positive_percent", 0),
                health.get("autograded_outcomes", 0),
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
        auto = _application().automation.snapshot(include_finished=False, limit=20)
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
        lines.append("  npu accelerator: %s" % npu_service.diagnostics_line())
    except Exception:
        lines.append("  npu accelerator: unknown (status unavailable)")
    try:
        tags = _get("/api/tags").get("models", [])
        names = sorted(m.get("name", "?") for m in tags)
        # Show the count AND an enumeration consistent with it: truncating the
        # list to 8 while printing "11 models" silently hid three models
        # (including sonder:latest, the active tier). Cap the enumeration but
        # make the omission explicit.
        shown = ", ".join(names[:8]) if names else "none"
        if len(names) > 8:
            shown += ", +%d more" % (len(names) - 8)
        lines.append("  ollama: ok (%d models: %s)" % (len(names), shown))
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
    except ModelCallError as error:
        return _format_model_call_error(error)
    except urllib.error.URLError as e:
        return f"ERROR contacting Ollama at {_ollama_display()}: {e}"

    installed = sorted(m.get("name", "?") for m in tags)
    loaded = [str(m.get("name")) for m in ps if m.get("name")]
    tier_lines = [
        f"  {k}={v}" + ("  [CLOUD - leaves machine]" if _is_cloud_tier(k, v) else "  [local Ollama]")
        for k, v in available_tiers(include_disabled=cloud_allowed()).items()
    ]
    if not ollama_endpoint.is_loopback(BASE):
        tier_lines = [
            line.replace("  [local Ollama]", "  [REMOTE OLLAMA - leaves machine]")
            for line in tier_lines
        ]
    lines = [
        "Unsafe lab mode: %s" % unsafe_lab.status_line(),
        f"Ollama @ {_ollama_display()} ({ollama_endpoint.locality(BASE)})",
        "Tiers:",
        *tier_lines,
        f"Learning tiers: {', '.join(sorted(LEARN_TIERS)) if LEARN_TIERS else '(none)'}",
        f"Installed/registered models: {', '.join(installed) if installed else '(none)'}",
        f"Resident in Ollama now: {', '.join(loaded) if loaded else '(none loaded)'}",
        f"local keep_alive: {KEEP_ALIVE}",
        "loopback retry: %d transient retry(s), %dms base delay; remote/cloud retries off" % (
            _local_model_retries(), int(_local_retry_delay(1) * 1000),
        ),
        "local runtime: threads={num_thread}, gpu_layers={num_gpu}, batch={num_batch}".format(
            **_local_runtime_summary()
        ),
    ]
    mcp_state = mcp_runtime_data()
    provenance = mcp_state.get("provenance") or {}
    if provenance.get("issue"):
        lines.append(
            "mcp runtime: ERROR %s (source root: %s)"
            % (
                provenance["issue"],
                "present" if provenance.get("source_root_exists") else "missing",
            )
        )
        action = _safe_mcp_recovery_action(provenance)
        if action:
            lines.append("mcp ACTION: %s" % action)
    try:
        auto = _application().automation.snapshot(include_finished=False, limit=20)
        lines.append(
            "autopilot: %s active, %s resumable"
            % (auto.get("active_runs", 0), auto.get("resumable_runs", 0))
        )
    except Exception as exc:
        lines.append("autopilot: ERROR %s" % exc)
    try:
        lines.append("npu accelerator: %s" % npu_service.diagnostics_line())
    except Exception:
        lines.append("npu accelerator: unknown (status unavailable)")
    try:
        spec = sonder_speculation.default_predictor().stats()
        lines.append(
            "branch predictor: %d predictions, %.0f%% accurate; "
            "speculation %d issued, %.0f%% retired (%d states); "
            "cost model decision~%.2fs tool~%.2fs, %.1fs hidden"
            % (
                spec["predictions"], spec["accuracy"] * 100,
                spec["speculations"], spec["speculation_hit_rate"] * 100,
                spec["transition_states"],
                spec["ewma_decision_s"], spec["ewma_tool_s"], spec["saved_s"],
            )
        )
    except Exception as exc:
        lines.append("branch predictor: ERROR %s" % exc)
    return "\n".join(lines)


# Ensemble ("ask several models, compound one answer") -------------------------
#
# Sequential by construction. This keeps peak RAM/VRAM predictable on CPU-only
# and accelerated hosts; concurrent model loads can otherwise thrash shared or
# discrete memory. Each model is unloaded after it answers so the next has room.
ENSEMBLE_MAX_MODELS = 4
# Vision needs an image channel this path does not have, and a VLM handed a
# text-only prompt answers with an immediate end-of-sequence.
ENSEMBLE_SKIP_TIERS = ("vision",)


def _ensemble_targets(tiers: str = ""):
    """Resolve the tiers to poll into a deduped [(tier, model)] list.

    Deduplicated by *resolved model*, not by tier name: several tiers routinely
    point at the same Ollama model (out of the box `code` and `general` are both
    `sonder:latest`), and asking one model the same question twice costs a full
    generation to learn nothing.
    """
    requested = [t.strip().lower() for t in (tiers or "").split(",") if t.strip()]
    explicit = bool(requested)
    if not requested:
        requested = [
            t for t in _configured_local_tiers() if t not in ENSEMBLE_SKIP_TIERS
        ]
    targets, seen_models, unknown = [], set(), []
    for tier in requested:
        if _is_cloud_tier(tier):
            # The implicit default must never silently ship the prompt
            # off-box. A cloud tier the caller NAMED, with cloud enabled, is
            # not silent -- that is consult's cloud leg and the /model
            # cloud-* routes, so include it. Named-but-disabled is reported,
            # not swallowed: the caller should see why the tier is absent.
            if not explicit:
                continue
            if not cloud_allowed():
                unknown.append("%s (cloud disabled; set SONDER_ALLOW_CLOUD=1)" % tier)
                continue
        model, _cloud, _augment, label = _serve_target(tier, False)
        if not model or label is None:
            unknown.append(tier)
            continue
        if model in seen_models:
            continue
        seen_models.add(model)
        targets.append((tier, model))
    return targets[:ENSEMBLE_MAX_MODELS], unknown


def _project_facts_text(project: str) -> str:
    """Durable facts for a project, as a constraints block.

    The ensemble builds its prompts directly rather than going through the
    learning orchestrator, so it never saw the facts sonder_remember_fact
    stores -- which is precisely where they are most useful, since the failure
    modes worth recording (wrong-library calls, generics written with
    parentheses, deleting code to silence a compiler) are all code-generation
    failures.
    """
    name = (project or "").strip()
    if not name or name.lower() == "none":
        return ""
    conn = _open_db()
    try:
        rows = memory_store.facts_for_project(conn, name)
    except Exception:
        return ""
    finally:
        conn.close()
    facts = [str(r.get("text", "")).strip() for r in rows or []]
    facts = [f for f in facts if f]
    if not facts:
        return ""
    return (
        "HARD CONSTRAINTS for this project. These are recorded from measured "
        "past failures; violating one is a known way this goes wrong:\n"
        + "\n".join("- " + f for f in facts)
        + "\n\n"
    )


def _ensemble_code_synthesis_prompt(question, answers):
    """Synthesis contract for code, where prose merging is actively harmful.

    Blending two source files line by line produces something that resembles
    both and compiles as neither, so this asks for a *pick and patch*: choose
    the more complete candidate as the base and take from the others only where
    the base is clearly missing or wrong.
    """
    numbered = "\n\n".join(
        "===== CANDIDATE %d (from the %s tier, model %s) =====\n%s"
        % (i, row["tier"], row["model"], row["answer"])
        for i, row in enumerate(answers, 1)
    )
    return (
        "Several models independently wrote the same source file. Produce the "
        "single best version.\n\n"
        "Rules:\n"
        "- Pick the most complete, most nearly correct candidate as your base.\n"
        "- Take a piece from another candidate ONLY where the base is missing it "
        "or is clearly wrong. Do not interleave them line by line.\n"
        "- The result must be ONE complete, self-contained, compilable file.\n"
        "- Output ONLY code. No prose, no markdown fences, no commentary, and no "
        "notes about which candidate you chose.\n"
        "- Do not leave TODOs, placeholders, or elided bodies.\n\n"
        "ORIGINAL REQUEST:\n%s\n\n%s\n\nFINAL FILE:" % (question, numbered)
    )


def _ensemble_synthesis_prompt(question, answers):
    numbered = "\n\n".join(
        "--- Answer %d (from the %s tier, model %s) ---\n%s"
        % (i, row["tier"], row["model"], row["answer"])
        for i, row in enumerate(answers, 1)
    )
    return (
        "Several local models were asked the same question independently. "
        "Compound their answers into one better answer.\n\n"
        "Rules:\n"
        "- Use only what the answers below contain. Do not introduce new facts.\n"
        "- Where they agree, state it once, plainly.\n"
        "- Where they disagree, say so explicitly and name which answer said "
        "what. Do not silently pick a side.\n"
        "- If one answer is clearly more complete, prefer it, but keep any "
        "correct detail the others add.\n"
        "- Answer the question directly. Do not describe this process.\n\n"
        "QUESTION:\n%s\n\n%s\n\nCOMPOUNDED ANSWER:" % (question, numbered)
    )


@mcp.tool()
def ensemble_answer(
    prompt: str,
    tiers: str = "",
    synth_tier: str = "",
    num_predict: int = 700,
    mode: str = "prose",
    project: str = "",
) -> str:
    """Ask several local models the same question, then compound one answer.

    Each model answers independently, then one model merges the answers --
    agreeing points stated once, disagreements named rather than hidden.

    Args:
        prompt: the question to put to every model.
        tiers: comma-separated tiers to poll. Default: every bound local text
            tier. Deduplicated by resolved model, capped at 4. A cloud tier
            named here joins the poll when SONDER_ALLOW_CLOUD=1; the implicit
            default never leaves the box.
        synth_tier: tier that writes the compounded answer. Default: the last
            tier that answered successfully.
        num_predict: output cap per model.
        mode: "prose" (default) merges the answers into one explanation.
            "code" switches to a pick-and-patch contract and returns a bare
            source file -- blending two implementations line by line yields
            something that resembles both and compiles as neither, and the
            prose contract's "name the disagreements" rule would emit
            commentary where a file is wanted.
        project: prepend that project's durable facts (sonder_remember_fact) to
            every model's prompt as hard constraints. The ensemble builds its
            prompts directly rather than through the learning orchestrator, so
            without this it never sees them -- and code generation is exactly
            where recorded failure modes pay off.
    """
    _maybe_live_reload()
    question = (prompt or "").strip()
    if not question:
        return "ERROR: ensemble_answer needs a prompt."
    # Prepend the project's durable facts. Every model in the ensemble sees
    # them, and so does the synthesis pass, since a merge that reintroduces a
    # constraint violation is as broken as generating one.
    question = _project_facts_text(project) + question

    targets, unknown = _ensemble_targets(tiers)
    if not targets:
        return "ERROR: no bound local tiers to poll%s." % (
            " (unknown: %s)" % ", ".join(unknown) if unknown else ""
        )

    answers, failures = [], []
    for tier, model in targets:
        started = time.monotonic()
        try:
            gen = _make_generate(model, "", 0.2, max(64, int(num_predict)), 4096)
            text = (gen(question) or "").strip()
        except ModelCallError as error:
            if error.kind == "cancelled":
                raise
            failures.append((tier, model, _format_model_call_error(error)))
            continue
        except Exception as exc:  # a bad tier must not sink the whole ensemble
            failures.append((tier, model, str(exc)))
            continue
        finally:
            # Free the card before loading the next one. Best effort: a failed
            # unload costs VRAM, not correctness. Cloud models hold no local
            # VRAM, so there is nothing to free.
            if not _is_cloud_tier(tier, model):
                try:
                    _post("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
                except Exception:
                    pass
        if text:
            answers.append({
                "tier": tier,
                "model": model,
                "answer": text,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
        else:
            failures.append((tier, model, "empty response"))

    if not answers:
        return "ERROR: no model produced an answer.\n%s" % "\n".join(
            "  %s (%s): %s" % row for row in failures
        )

    footer_rows = [
        "  %s (%s) in %dms" % (r["tier"], r["model"], r["elapsed_ms"])
        for r in answers
    ]
    footer_rows += ["  %s (%s): FAILED - %s" % row for row in failures]
    footer = "\n=== ENSEMBLE (%d model%s answered) ===\n%s" % (
        len(answers), "" if len(answers) == 1 else "s", "\n".join(footer_rows)
    )

    # In code mode the return value is a source file, so the provenance footer
    # would be pasted straight into it. Report to the log instead.
    code_mode = str(mode or "").strip().lower() == "code"
    if code_mode:
        activity_tracker.record_event(
            "ensemble",
            summary="%d model(s) answered: %s" % (
                len(answers), ", ".join(r["tier"] for r in answers)
            ),
        )
        footer = ""

    if len(answers) == 1:
        # Nothing to compound. Returning the single answer is honest; running a
        # synthesis pass over one input would only launder it.
        return answers[0]["answer"] + footer

    synth = (synth_tier or "").strip().lower() or answers[-1]["tier"]
    synth_model, _cloud, _augment, synth_label = _serve_target(synth, False)
    if not synth_model or synth_label is None:
        synth_model = answers[-1]["model"]
        synth = answers[-1]["tier"]
    build_prompt = (
        _ensemble_code_synthesis_prompt if code_mode else _ensemble_synthesis_prompt
    )
    try:
        gen = _make_generate(synth_model, "", 0.2, max(256, int(num_predict)), 8192)
        merged = (gen(build_prompt(question, answers)) or "").strip()
    except Exception as exc:
        # Synthesis is the only step that can fail after real work is done, so
        # hand back the strongest single answer rather than losing everything.
        if code_mode:
            activity_tracker.record_event(
                "ensemble",
                summary="synthesis failed (%s); using the longest candidate" % exc,
            )
            return max(answers, key=lambda r: len(r["answer"]))["answer"]
        raw = "\n\n".join(
            "--- %s (%s) ---\n%s" % (r["tier"], r["model"], r["answer"])
            for r in answers
        )
        return "%s\n\n(synthesis failed: %s)%s" % (raw, exc, footer)

    if not merged:
        return answers[-1]["answer"] + footer
    if code_mode:
        return merged
    return "%s\n%s  synthesized by %s (%s)" % (merged, footer, synth, synth_model)


@mcp.tool()
def consult(
    prompt: str,
    tiers: str = "",
) -> str:
    """Ask several tiers independently and expose agreement as a confidence signal.

    This deliberately returns every answer and an agree/disagree verdict. It
    never synthesizes them or chooses a winner: measured ensembles did not
    improve accuracy, while divergence is useful evidence that a caller should
    verify the answer. Two good answers still yield a verdict even if a third
    tier fails; if the judge fails, a token-overlap fallback is labeled
    unknown-confidence.

    By default it contrasts configured LOCAL base/specialist models and joins a
    cloud model (cloud-general) whenever
    cloud is enabled (SONDER_ALLOW_CLOUD=1) -- so the cloud is used when
    available but a disabled cloud never blocks the second opinion.

    Args:
        prompt: the identical question to ask every tier.
        tiers: optional comma-separated tier override (e.g. "code,reasoning");
            empty uses the adaptive local+local+cloud default.
    """
    _maybe_live_reload()
    chosen = [t.strip() for t in tiers.split(",") if t.strip()] or None
    result = consult_flow.consult(prompt, chosen)
    return consult_flow.format_result(result)


@mcp.tool()
def route_request(prompt: str) -> str:
    """Suggest the tier best suited to a request, and say why.

    The one durable model finding here: a local model is strong when the facts
    are in the prompt (transformation) and weak when it must remember one
    (recall -- an API signature, a lookup table). This classifies the request
    on that axis and names the tier measured best for it, so the routing choice
    is legible rather than magic. It is a suggestion; the caller may override.
    """
    _maybe_live_reload()
    decision = tier_router.route(prompt, available_tiers=set(TIERS))
    return (
        "kind: %s\ntier: %s\nreason: %s"
        % (decision["kind"], decision["tier"], decision["reason"])
    )


@mcp.tool()
def improve_function(
    path: str,
    function: str,
    objective: str = "",
    tier: str = "",
    apply: bool = False,
) -> str:
    """Propose a guarded improvement to ONE function, shown as a diff.

    Asks the model for exactly one function -- the transformation shape it is
    reliable at -- and splices only that function back. The change is then run
    through the guards this project measured catching plausible-but-wrong edits
    that passed a green test suite: a comment-only diff, a rewritten
    return/raise (a contract change), an invented numeric restriction, a
    defaulted lookup turned strict, a net-new print, and a deletion below 75%
    of the original. A candidate that trips any guard is rejected with the
    reason, not applied.

    Returns the unified diff and the verdict. With apply=False (the default)
    nothing is written -- read the diff and decide. With apply=True the file is
    written through the same guarded file path as every other write, so root
    and approval gates apply. Auto-routes the tier by request kind when tier is
    empty.
    """
    _maybe_live_reload()
    try:
        data = file_ops.read_file(path)
        source = data.get("text", "") if isinstance(data, dict) else str(data)
    except Exception as exc:
        return "ERROR: could not read %s: %s" % (path, exc)
    if not source.strip():
        return "ERROR: %s is empty or unreadable" % path

    chosen = tier or tier_router.route(
        objective or "improve the %s function" % function,
        available_tiers=set(TIERS),
    )["tier"]

    def ask(prompt_text, model_tier):
        return ensemble_answer(prompt_text, tiers=model_tier, mode="code")

    result = code_improve.improve_function(
        source, function, ask, tier=chosen, objective=objective)
    if not result["ok"]:
        return "no change: %s (tier=%s)" % (result["reason"], chosen)

    header = "function: %s\ntier: %s\nobjective: %s\n" % (
        function, chosen, objective or "(model chose)")
    if not apply:
        return "%s\n%s\napply with improve_function(..., apply=True)" % (
            header, result["diff"])

    write = file_ops.write_file(path, result["edited"], mode="overwrite")
    ok = write.get("ok", True) if isinstance(write, dict) else True
    return "%s\nAPPLIED to %s (%s)\n\n%s" % (
        header, path, "ok" if ok else "write reported a problem", result["diff"])


@mcp.tool()
def environment_status(refresh: bool = False) -> str:
    """Report the host environment: OS, shells, and installed toolchains.

    Deterministic discovery (shutil.which/platform -- no subprocesses), so an
    agent or user can see which platform this runtime is on, which shell to
    prefer (PowerShell on Windows, bash elsewhere), and which interpreters and
    build tools actually exist before choosing a command shape. The workbench
    agent already receives a one-line brief of this on every run; this tool is
    the full listing. refresh=True re-probes after installing something.
    """
    _maybe_live_reload()
    return environment_probe.format_profile(refresh=refresh)


@mcp.tool()
def hardware_profile(workload: str = "general", refresh: bool = False) -> str:
    """Report accelerator inventory and conservative local-model fit.

    Enumerates NVIDIA, AMD, Intel, Apple, and unknown display accelerators with
    bounded platform-native probes. Detection does not assert that an Ollama,
    CUDA, ROCm, Vulkan, Metal, or other backend is usable. Recommendations are
    read-only capacity plans; they never change drivers or runtime settings.
    Set refresh=True after a hardware/driver change to bypass the process cache.
    """
    _maybe_live_reload()
    return sonder_hardware.profile_text(workload=workload, refresh=refresh)


@mcp.tool()
def scaffold_project(
    kind: str,
    name: str,
    root: str = "",
    apply: bool = False,
) -> str:
    """Emit a complete, deterministic project skeleton for one language.

    Solution/build-file plumbing (.sln GUID blocks, .vcxproj configuration,
    pyproject/Cargo/pom boilerplate) is pure recall and the measured worst
    case for a local model -- asked for "a full MSVC project" it produced good
    code and no .sln at all. This tool owns those formats as templates, so a
    model (or a user) only supplies the two facts that matter: the kind and
    the name. No model call is involved.

    Kinds: cpp-msvc, cpp-cmake, csharp, rust, python, node, go, java-maven
    (aliases like c++, c#, js, py, cmake work too).

    With apply=False (default) it returns the full file listing as a preview.
    With apply=True it writes each file under `root` through the same guarded
    file path as every other write (mode=create -- an existing file is an
    error, a scaffold never clobbers), so filesystem roots and approval gates
    apply. `root` is required to apply.
    """
    _maybe_live_reload()
    try:
        files = project_scaffold.render(kind, name)
    except ValueError as exc:
        return "ERROR: %s" % exc

    canonical = project_scaffold.normalize_kind(kind)
    if not apply:
        sections = ["scaffold preview: kind=%s name=%s (%d files)"
                    % (canonical, name, len(files))]
        for rel in sorted(files):
            sections.append("--- %s ---\n%s" % (rel, files[rel] or "(empty)"))
        sections.append("apply with scaffold_project(..., root=<dir>, apply=True)")
        return "\n\n".join(sections)

    if not str(root or "").strip():
        return "ERROR: root is required to apply a scaffold"
    written, failures = [], []
    for rel in sorted(files):
        target = os.path.join(str(root).strip(), rel.replace("/", os.sep))
        try:
            file_ops.write_file(target, files[rel], mode="create")
            written.append(target)
        except Exception as exc:
            failures.append("%s: %s" % (target, exc))
    lines = ["scaffold: kind=%s name=%s" % (canonical, name)]
    lines += ["  wrote %s" % path for path in written]
    lines += ["  FAILED %s" % failure for failure in failures]
    if failures:
        lines.append("result: incomplete -- %d of %d files failed"
                     % (len(failures), len(files)))
    else:
        lines.append("result: complete (%d files)" % len(written))
    return "\n".join(lines)


def _codegen_build(program, args_json, cwd, timeout, token, approval, extra_roots):
    """Run the project's own build; return (combined output, exited cleanly).

    Every branch that did not actually compile the code says so in words
    codegen_loop.build_ran() recognises. Without that, an infrastructure
    failure was scored as a candidate: "error: build could not run: ..."
    matches the error regex, counted as exactly ONE error in the trustworthy
    tier, and beat an honest candidate with thirty real errors -- so a build
    that never launched won, and every later attempt was compared against it.

    The second element is the build process's own verdict, which used to be
    dropped here. Success was then derived purely from "no line matched
    error_regex", so a `dotnet build` that exited 1 on `error NU1101` under a
    stricter CS\\d{4} regex -- or any toolchain whose failure text the regex does
    not know -- reported BUILD SUCCEEDED for a project that never compiled.
    """
    try:
        data = workbench.run_program(
            program,
            args_json=args_json,
            cwd=cwd,
            timeout=timeout,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        return "error: build could not run: %s" % exc, False
    if not isinstance(data, dict):
        return str(data), False
    stdout = data.get("stdout", "") or ""
    stderr = data.get("stderr", "") or ""
    parts = []
    # A killed build reports whatever it managed to print, which can be nothing
    # at all -- and an empty error list reads as a clean compile. Say it
    # explicitly rather than let silence mean success. workbench clamps the
    # requested timeout to its own maximum, so this fires on ordinary builds,
    # not just pathological ones.
    if data.get("timed_out"):
        parts.append("error: build timed out after %ss" % data.get("timeout", timeout))
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        # The captured window keeps the head, and MSBuild prints its error
        # summary at the tail, so the errors are exactly what gets dropped.
        parts.append("error: build output was truncated; the error summary may be missing")
    parts.append(stdout)
    parts.append(stderr)
    return "\n".join(p for p in parts if p), bool(data.get("ok"))


@mcp.tool()
def codegen_build_loop(
    project_dir: str,
    files_json: str,
    build_program: str,
    build_args_json: str = "[]",
    tiers: str = "",
    attempts: int = 2,
    num_predict: int = 3000,
    error_regex: str = "",
    slips_json: str = "",
    timeout: int = 900,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Write code, compile it, and repair it against the real compiler.

    Generates one file at a time from a per-file spec, runs the project's own
    build after each, and keeps whichever version leaves the WHOLE project with
    the fewest errors. Later files are shown the API extracted from the files
    already written, not an idealised contract, because a local model cannot
    hold an agreement spanning files even when told it every time.

    Guards that are not optional (each one is here because the unguarded loop
    was measured doing the opposite):
      - a replacement that shrinks a file below 75% is rejected as deletion
        rather than repair;
      - a file that already builds clean is never regenerated;
      - versions are scored on total project errors, not the file's own;
      - known wrong-library calls are rewritten mechanically, not by asking.

    A green build here is NOT proof the program works: a declared-but-never
    assigned field is not a compile error. Run the project's tests too.

    Args:
        project_dir: directory holding the project. Must be inside an allowed root.
        files_json: {"name.ext": "what this file must contain"} in dependency
            order, earliest first, or a list of {"name", "spec"} objects.
        build_program: the build executable, e.g. "dotnet", "cargo", "make".
        build_args_json: argv for it as JSON, e.g. ["build", "-c", "Release"].
        tiers: comma-separated model tiers to ensemble. Default: all bound local tiers.
        attempts: tries per file; the best-scoring one is kept.
        error_regex: how to recognise an error line in build output. Defaults to
            a generic error/fatal match; pass a stricter one for a noisy build.
        slips_json: [[regex, replacement], ...] rewrites applied to generated
            code, for wrong-library calls the model repeats.
    """
    _maybe_live_reload()
    try:
        wanted = codegen_loop.parse_files(files_json)
        slips = codegen_loop.parse_slips(slips_json)
    except ValueError as exc:
        return "ERROR: %s" % exc
    if not wanted:
        return "ERROR: files_json listed no files."
    error_regex = error_regex or codegen_loop.DEFAULT_ERROR_RE
    try:
        re.compile(error_regex)
    except re.error as exc:
        return "ERROR: bad error_regex: %s" % exc

    # Set when a build fails to launch or is killed. Such a build says nothing
    # about the code, so its error list must never be scored against a real
    # candidate and must never be read as a pass. `exit_ok` is the build's own
    # verdict: a build that ran and failed is not a pass either, however few of
    # its lines the error regex happened to recognise.
    build_state = {"ran": True, "exit_ok": True}

    def run_build():
        out, exit_ok = _codegen_build(build_program, build_args_json, project_dir,
                                      timeout, token, approval, extra_roots)
        build_state["ran"] = codegen_loop.build_ran(out)
        build_state["exit_ok"] = exit_ok
        errors = codegen_loop.count_errors(out, error_regex)
        if not exit_ok and not errors:
            # The compiler said no and the regex heard nothing -- a restore
            # failure under a CS-only regex, a non-English toolchain, a Gradle
            # "FAILURE:" banner. An empty list here read as a clean compile, so
            # say what the process actually reported instead of inventing a pass.
            errors = [
                "error: the build exited with a failure status but no output "
                "line matched error_regex"
            ]
        return errors

    def read(name):
        try:
            data = file_ops.read_file(
                os.path.join(project_dir, name), extra_roots=extra_roots,
                bypass=_file_bypass_allowed(token, approval),
            )
            # read_file returns {"path","bytes","truncated","text"} -- there is no
            # "content" key, so reading one made `existing` unconditionally empty
            # and silently disabled every guard that depends on knowing what is
            # already on disk: the shrink floor could not fire (shrink_rejected
            # returns False with no incumbent), a clean file was regenerated
            # every run, the first attempt was accepted unscored, and siblings
            # carried no API for the next file.
            return data.get("text", "") if isinstance(data, dict) else str(data)
        except Exception:
            return ""

    def write(name, content):
        return file_ops.write_file(
            os.path.join(project_dir, name), content, mode="overwrite",
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )

    rows = []
    for name, spec in wanted:
        existing = read(name)
        errors = run_build()
        mine = [e for e in errors if name in e]
        # "No errors named this file" only means "clean" if the compiler
        # actually reached this file. Under a masked build it reached nothing,
        # so EVERY file looks clean: measured, one parse error in one file made
        # the loop skip all six remaining files and report FINAL: 1 error --
        # a no-op run that read as near-success.
        masked = codegen_loop.count_unreliable(errors)
        # ...and only if the build itself succeeded: a failing build whose
        # errors name some other file (or no file the regex recognises) is not
        # evidence that this one is clean.
        if existing and not mine and not masked and build_state["exit_ok"]:
            rows.append({"name": name, "note": "already clean, not regenerated"})
            continue

        best_code = existing or None
        best_total = codegen_loop.score(errors) if existing else None
        note = "unchanged"
        siblings = {n: read(n) for n, _ in wanted if n != name}
        siblings = {n: t for n, t in siblings.items() if t.strip()}

        for attempt in range(1, max(1, int(attempts)) + 1):
            prompt = "%s\n%s\nOutput only the contents of %s. Code only." % (
                codegen_loop.dependency_brief(siblings), spec, name,
            )
            reply = ensemble_answer(prompt, tiers=tiers, num_predict=num_predict, mode="code")
            code = codegen_loop.strip_code(reply)
            code, hits = codegen_loop.apply_slips(code, slips)

            if codegen_loop.shrink_rejected(existing, code):
                note = "attempt %d rejected: shrank to %d%% of the original" % (
                    attempt, 100 * len(code) // max(1, len(existing)),
                )
                continue
            try:
                write(name, code)
            except Exception as exc:
                return "ERROR: could not write %s: %s" % (name, exc)

            attempt_errors = run_build()
            if not build_state["ran"]:
                # Nothing was compiled, so this attempt is not evidence about
                # the code and must not be scored. Stop rather than keep
                # generating against a build that cannot run.
                note = "attempt %d abandoned: the build did not run" % attempt
                break
            attempt_score = codegen_loop.score(attempt_errors)
            if best_total is None or attempt_score < best_total:
                best_code, best_total = code, attempt_score
                note = "attempt %d kept (%s%s)" % (
                    attempt, codegen_loop.describe_total(attempt_errors),
                    ", %d slip(s) rewritten" % hits if hits else "",
                )
            if not attempt_errors:
                break

        if best_code is not None:
            try:
                write(name, best_code)
            except Exception as exc:
                return "ERROR: could not restore %s: %s" % (name, exc)
        rows.append({"name": name, "note": note})

    final = run_build()
    return codegen_loop.format_report(
        rows, final,
        ok=not final and build_state["ran"] and build_state["exit_ok"],
        ran=build_state["ran"],
    )


@mcp.tool()
def unload(tier: str = "all") -> str:
    """Immediately free GPU VRAM by unloading a model (or all of them).

    Args:
        tier: "all" (default), or any configured local tier ("fast", "code",
            "general", and "reasoning"/"vision" when bound).
    """
    _maybe_live_reload()
    if tier == "all":
        # Only local tiers occupy VRAM; cloud tiers run remote.
        targets = list(dict.fromkeys(
            v for k, v in TIERS.items() if not _is_cloud_tier(k, v)
        ))
    elif _is_cloud_tier(tier):
        return f"'{tier}' is a cloud tier — it uses no local VRAM, nothing to unload."
    else:
        targets = [TIERS.get(tier)]
    if None in targets:
        return f"ERROR: unknown tier '{tier}'. Valid: all, {_valid_tier_names()}."
    active = master_orchestrator.active_model_call_count()
    if active:
        return (
            "ERROR: unload deferred while %d fleet model call(s) are active; "
            "cancel/wait for master_status() to reach zero, then retry."
        ) % active
    requested = []
    errors = []
    for model in targets:
        try:
            response = _post("/api/generate", {"model": model, "keep_alive": 0})
            if isinstance(response, dict) and response.get("error"):
                errors.append("%s: %s" % (
                    model, _safe_model_error_detail(response.get("error"), limit=200),
                ))
            else:
                requested.append(model)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            errors.append("%s: %s" % (model, _transport_error_detail(exc)))

    resident = set()
    residency_error = ""
    try:
        deadline = time.monotonic() + 5.0
        while True:
            residency_payload = _get("/api/ps")
            if (
                not isinstance(residency_payload, dict)
                or not isinstance(residency_payload.get("models"), list)
            ):
                raise ValueError("invalid Ollama /api/ps response")
            resident = ollama_lifecycle.resident_models(residency_payload)
            if not any(model.casefold() in resident for model in requested):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        residency_error = _transport_error_detail(exc)
    remaining = [model for model in requested if model.casefold() in resident]

    cleanup = ollama_lifecycle.cleanup_orphaned_discovery_probes(
        # Only an explicit all-tier unload with an authoritative empty Ollama
        # residency list may reclaim old orphaned model runners. A specific
        # tier unload or failed /api/ps check protects them for manual review.
        allow_model_runners=(
            tier == "all" and not residency_error and not resident
        ),
    )
    lines = [
        "Unload requested for: %s." % (", ".join(requested) if requested else "(none)"),
    ]
    if remaining:
        lines.append("WARNING: still resident in Ollama: %s." % ", ".join(remaining))
    elif residency_error:
        lines.append("WARNING: residency could not be confirmed: %s." % residency_error)
    elif requested:
        lines.append("Ollama residency confirmed clear for the requested model(s).")
    else:
        lines.append("WARNING: no unload request was accepted by Ollama.")
    if cleanup["terminated"]:
        lines.append(
            "Cleaned orphaned Ollama GPU-discovery probe PID(s): %s."
            % ", ".join(str(pid) for pid in cleanup["terminated"])
        )
    if cleanup["terminated_model_runners"]:
        lines.append(
            "Cleaned verified orphaned Ollama model runner PID(s): %s."
            % ", ".join(
                str(pid) for pid in cleanup["terminated_model_runners"]
            )
        )
    if cleanup["protected_model_runners"]:
        lines.append(
            "WARNING: orphaned model runner PID(s) were not terminated automatically: %s."
            % ", ".join(str(pid) for pid in cleanup["protected_model_runners"])
        )
    for error in errors + cleanup["errors"]:
        lines.append("WARNING: %s" % error)
    return "\n".join(lines)


# MCP is more than model-controlled tools. These small, passive resources let
# clients attach live runtime facts without spending a tool turn, while prompts
# make the safest high-value workflows discoverable in every MCP client.
@mcp.resource(
    "sonder://runtime/status",
    name="runtime-status",
    title="Sonder Runtime Status",
    description="Live local model tiers, residency, and controller state.",
    mime_type="text/plain",
)
def _resource_runtime_status() -> str:
    return status()


@mcp.resource(
    "sonder://runtime/diagnostics",
    name="runtime-diagnostics",
    title="Sonder Runtime Diagnostics",
    description="Read-only health checks for policy, memory, models, and MCP state.",
    mime_type="text/plain",
)
def _resource_runtime_diagnostics() -> str:
    return diagnostics()


@mcp.resource(
    "sonder://runtime/environment",
    name="host-environment",
    title="Host Environment",
    description="Detected OS, shells, interpreters, and build toolchains.",
    mime_type="text/plain",
)
def _resource_host_environment() -> str:
    return environment_status()


@mcp.resource(
    "sonder://runtime/tools",
    name="tool-manifest",
    title="Sonder Tool Manifest",
    description="Compact deterministic index of Sonder's model-callable tools.",
    mime_type="text/plain",
)
def _resource_tool_manifest() -> str:
    return tool_manifest()


@mcp.prompt(
    name="implement_repository_task",
    title="Implement a Repository Task Safely",
    description="A verification-first workflow for bounded repository changes.",
)
def _prompt_implement_repository_task(objective: str, project: str = ".") -> str:
    return (
        "Work on this repository task: %s\n\n"
        "Host-selected project root: %s\n"
        "First inspect the relevant code, repository status, and local instructions. "
        "State the narrow file ownership boundary, preserve unrelated changes, and use "
        "guarded repository tools only. Implement the smallest complete change, add the "
        "test that would have caught the defect, run focused verification, then report "
        "exact files changed, evidence, and anything still unverified. Never claim a "
        "build or test that did not run." % (objective, project)
    )


@mcp.prompt(
    name="review_change",
    title="Adversarial Change Review",
    description="Review a proposed change for correctness, security, and missing tests.",
)
def _prompt_review_change(change: str, focus: str = "correctness, security, tests") -> str:
    return (
        "Review the following proposed change adversarially. Focus on %s. Trace concrete "
        "inputs through changed branches, identify API/ownership/concurrency/security "
        "regressions, distinguish verified facts from inference, and return prioritized "
        "findings with exact evidence. If there are no findings, say what you inspected "
        "and which runtime boundaries remain unverified.\n\nCHANGE:\n%s" % (focus, change)
    )


@mcp.prompt(
    name="grounded_research",
    title="Grounded Multi-Source Research",
    description="Research a question with source, freshness, and uncertainty discipline.",
)
def _prompt_grounded_research(question: str, constraints: str = "") -> str:
    return (
        "Research this question: %s\n\nConstraints: %s\n"
        "Prefer primary/current sources, separate sourced facts from inference, record "
        "dates for drift-prone claims, expose disagreement, and do not fill missing facts "
        "from model recall. End with the answer, direct source links, and unresolved "
        "uncertainty." % (question, constraints or "none")
    )


@mcp.prompt(
    name="debug_failure",
    title="Evidence-First Failure Debugging",
    description="Trace the first failing invariant before proposing a repair.",
)
def _prompt_debug_failure(symptom: str, evidence: str = "") -> str:
    return (
        "Debug this failure: %s\n\nAvailable evidence:\n%s\n\n"
        "Preserve the first failure, reproduce with the smallest safe check, trace the "
        "first violated invariant rather than downstream errors, compare with the last "
        "known-good path when available, and propose a fix only after the cause is "
        "supported. Report the verification boundary explicitly." % (symptom, evidence)
    )


mcp.finish_module_refresh(__name__, __file__, globals())


def run_mcp() -> None:
    """Run the MCP adapter only after the process-level lab gate succeeds."""
    unsafe_lab.require_startup()
    mcp.run()


if __name__ == "__main__" and not globals().get("_MCP_HOT_RELOAD_EXEC"):
    run_mcp()
