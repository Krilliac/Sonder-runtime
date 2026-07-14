"""Master/subagent orchestration with live status snapshots."""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import itertools
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import fleet_store


# Preserve process-local execution state across importlib.reload(). The durable
# ledger is intentionally owned by the non-hot-reloaded fleet_store service.
if "_LOCK" not in globals():
    _LOCK = threading.RLock()
if "_OWNER_SERVICE_LOCK" not in globals():
    _OWNER_SERVICE_LOCK = threading.RLock()
if "_AGENTS" not in globals():
    _AGENTS = {}
if "_EVENTS" not in globals():
    _EVENTS = []
if "_UPDATE_SEQUENCE" not in globals():
    _UPDATE_SEQUENCE = itertools.count()
if "_WORKER_LOCAL" not in globals():
    _WORKER_LOCAL = threading.local()
if "_WORKER_FAILED" not in globals():
    _WORKER_FAILED = object()
if "_OWNER_ID" not in globals():
    _OWNER_ID = "owner-%s-%s" % (os.getpid(), uuid.uuid4().hex[:12])
    _OWNER_STARTED_TS = time.time()
    _OWNER_REGISTERED = False
    _HEARTBEAT_THREAD = None
    _HEARTBEAT_STOP = threading.Event()
    _STORE_ERROR = ""
    _ATEXIT_REGISTERED = False
_MAX_EVENTS = 80
DEFAULT_MAX_AGENTS = 16
ABSOLUTE_MAX_AGENTS = 64
DEFAULT_MAX_WORKERS = 8
ABSOLUTE_MAX_WORKERS = 16
RAM_RESERVE_BYTES = int(1.5 * 1024 ** 3)
# A "worker" is a Python thread that POSTs to Ollama and waits -- the model
# weights live in the ollama server process (and in VRAM), NOT once per worker.
# This budget therefore covers only the thread's own prompt/response buffers.
# (It was 1.25 GiB, i.e. a whole model per worker, which silently pinned every
# GPU box to 2-3 slots for a cost that is not actually paid here.)
RAM_PER_WORKER_BYTES = int(0.25 * 1024 ** 3)
# VRAM held back for the display/compositor and allocator slack.
GPU_RESERVE_BYTES = int(0.5 * 1024 ** 3)
# Ollama loads ONE copy of the model and batches concurrent requests against
# it, so extra concurrency costs a per-sequence KV cache, not another model.
# ~0.5 GiB covers a 7B-class model at an 8k context; smaller models/contexts
# leave more headroom and therefore allow more parallel sequences.
GPU_KV_CACHE_PER_WORKER_BYTES = int(0.5 * 1024 ** 3)
_GPU_QUERY_CACHE_SECONDS = 15.0
HEARTBEAT_SECONDS = 5
ABORT_MARKERS = ("CANCELLED", "INTERRUPTED")

EVIDENCE_REQUIRED = (
    "EVIDENCE_REQUIRED: guarded source evidence was unavailable. Authorize the "
    "repository in file_roots.local, embed the relevant source excerpts, or use "
    "the tool-using agent surface to inspect it first. No unsupported answer was produced."
)

_REPOSITORY_REQUEST = re.compile(
    r"(?:\brepository\s*:|\brepo\s*:|\bcurrent\s+(?:file|code|diff|uncommitted)|"
    r"\b(?:inspect|read|review|audit|edit|fix)\b.{0,40}\b(?:repo|repository|codebase|"
    r"workspace|files?)\b|\buse\s+(?:local\s+)?file[- ]reading\s+tools?\b)",
    re.IGNORECASE | re.DOTALL,
)
_EMBEDDED_EVIDENCE = re.compile(
    r"(?:```|\bsource\s+excerpts?\s*:|\bbegin\s+file\b|\bpatch\s*:|\bdiff\s+--git\b)",
    re.IGNORECASE,
)
# An ABSOLUTE path to a concrete source/text file names existing repository
# state to inspect, whatever the verb. Without this, "generate a summary of the
# file D:\repo\X.cpp" missed the repository lane (its verb is not read/inspect/
# ...) and routed to an ungrounded generation path that fabricated the file's
# contents. Relative paths (output.txt, models/foo.glb) are NOT matched, so
# greenfield "write to <relative>" tasks stay creative.
_REPO_FILE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]+\."
    r"(?:py|js|ts|tsx|jsx|cpp|cc|cxx|c|h|hpp|hxx|cs|java|go|rs|rb|php|swift|kt|"
    r"md|txt|rst|json|ya?ml|toml|ini|cfg|conf|xml|html?|css|scss|sh|ps1|bat|"
    r"cmake|gradle|sql|proto|glsl|hlsl)\b",
    re.IGNORECASE,
)
_FLEET_REQUEST = re.compile(
    r"(?:\bfleet\b|\bswarm\b|\bfan[- ]?out\b|\bparallel\s+agents?\b|"
    r"\bspawn\s+(?:as\s+much|as\s+many|the\s+maximum|maximum|parallel|subagents?)\b|"
    r"\bas\s+many\s+(?:subagents?|agents?)\b|\bparallel\s+workflow\b|"
    r"\bspawn\s+workflow\b|\bworkflow\b|\bmax(?:imum)?\s+agents?\b)",
    re.IGNORECASE,
)


def _remember_store_error(exc) -> None:
    global _STORE_ERROR
    _STORE_ERROR = "%s: %s" % (exc.__class__.__name__, exc)


def _heartbeat_enabled() -> bool:
    return os.environ.get("SONDER_FLEET_HEARTBEAT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _has_local_active_agents() -> bool:
    with _LOCK:
        return any(
            row.get("status") in ("queued", "running")
            for row in _AGENTS.values()
        )


def _heartbeat_loop() -> None:
    while not _HEARTBEAT_STOP.wait(HEARTBEAT_SECONDS):
        if not _has_local_active_agents():
            continue
        try:
            if not fleet_store.heartbeat_owner(_OWNER_ID):
                fleet_store.register_owner(
                    _OWNER_ID, os.getpid(), _OWNER_STARTED_TS,
                )
        except Exception as exc:  # durability must not kill an active model call
            _remember_store_error(exc)


def _ensure_owner() -> None:
    global _OWNER_REGISTERED, _HEARTBEAT_THREAD, _ATEXIT_REGISTERED
    with _OWNER_SERVICE_LOCK:
        if not _OWNER_REGISTERED:
            fleet_store.register_owner(_OWNER_ID, os.getpid(), _OWNER_STARTED_TS)
            _OWNER_REGISTERED = True
        if _heartbeat_enabled() and (
            _HEARTBEAT_THREAD is None or not _HEARTBEAT_THREAD.is_alive()
        ):
            _HEARTBEAT_THREAD = threading.Thread(
                target=_heartbeat_loop,
                name="sonder-fleet-heartbeat",
                daemon=True,
            )
            _HEARTBEAT_THREAD.start()
        if not _ATEXIT_REGISTERED:
            atexit.register(_close_owner)
            _ATEXIT_REGISTERED = True


def _close_owner() -> None:
    if not _OWNER_REGISTERED:
        return
    _HEARTBEAT_STOP.set()
    try:
        fleet_store.close_owner(_OWNER_ID)
    except Exception:
        pass


def hardware_max_agents() -> int:
    """Return the local queued-candidate ceiling from logical CPU capacity.

    This controls breadth/diversity, not simultaneous model calls. Concurrent
    execution is separately constrained by :func:`capacity`. The default queues
    two candidates per logical CPU, capped by the global safety limit.
    ``SONDER_MAX_AGENTS`` can lower or raise it up to that safety limit.
    """
    logical = max(1, int(os.cpu_count() or 1))
    return max(DEFAULT_MAX_AGENTS, min(ABSOLUTE_MAX_AGENTS, logical * 2))


def physical_memory_bytes() -> tuple[int, int]:
    """Return ``(total, available)`` physical RAM, or zeros if unavailable."""
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.length = ctypes.sizeof(MemoryStatusEx)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            ok = False
        if ok:
            return int(status.total_physical), int(status.available_physical)
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = page_size * int(os.sysconf("SC_PHYS_PAGES"))
        available = page_size * int(os.sysconf("SC_AVPHYS_PAGES"))
        return total, available
    except (AttributeError, OSError, TypeError, ValueError):
        return 0, 0


def gpu_memory_bytes() -> tuple[int, int]:
    """Return ``(total, free)`` VRAM of the largest local GPU, or zeros.

    Zeros mean "no GPU detected / unknown" -- callers must treat that as
    "do not constrain on VRAM" rather than as a zero-capacity GPU.
    """
    cached = globals().get("_GPU_MEM_CACHE")
    if cached and (time.time() - cached[0]) < _GPU_QUERY_CACHE_SECONDS:
        return cached[1], cached[2]
    total = free = 0
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
            # Pick the largest GPU when several are present.
            for line in (out.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    continue
                mib_total, mib_free = int(parts[0]), int(parts[1])
                if mib_total > total // (1024 ** 2):
                    total = mib_total * 1024 ** 2
                    free = mib_free * 1024 ** 2
    except (OSError, ValueError, subprocess.SubprocessError):
        total = free = 0
    globals()["_GPU_MEM_CACHE"] = (time.time(), total, free)
    return total, free


def fleet_model_bytes() -> int:
    """On-disk size of the model the fleet lane runs, or 0 if unknown.

    Ollama's resident footprint tracks this closely enough to budget VRAM
    against; an unknown model (0) simply means "don't constrain on VRAM".
    """
    cached = globals().get("_FLEET_MODEL_CACHE")
    if cached and (time.time() - cached[0]) < _GPU_QUERY_CACHE_SECONDS:
        return cached[1]
    size = 0
    try:
        import runtime_policy  # local import: runtime_policy never imports this module

        policy = runtime_policy.load(create=True)
        tier = runtime_policy.route_tier("fleet", policy=policy)
        wanted = str((policy.get("local_models") or {}).get(tier) or "").strip()
        if wanted:
            host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
            host = host.replace("0.0.0.0", "127.0.0.1")
            if not host.startswith("http"):
                host = "http://" + host
            with urllib.request.urlopen(host + "/api/tags", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for model in payload.get("models") or []:
                if str(model.get("name") or "").strip() == wanted:
                    size = int(model.get("size") or 0)
                    break
    except Exception:
        size = 0
    globals()["_FLEET_MODEL_CACHE"] = (time.time(), size)
    return size


def ollama_parallel_limit() -> int:
    """``OLLAMA_NUM_PARALLEL`` as the server would see it, or 0 when unset.

    This is the number of sequences the Ollama server will actually batch
    against the resident model. It is the hard ceiling on real concurrency:
    Sonder can hand N requests to Ollama, but if Ollama's parallelism is 1
    they queue and every extra worker slot buys exactly nothing (measured on
    this box 2026-07-13: with it unset, 2 concurrent requests took 2.07x a
    single one, and 4 took 4.18x -- perfectly serial).

    0 means "unset": Ollama auto-selects, which on a VRAM-tight box silently
    collapses to 1. :func:`capacity` reports that as a warning rather than
    pretending the slots are real.

    Falls back to the persisted Windows user environment when our own process
    does not carry the variable -- the Ollama server reads that same store at
    launch, and Sonder is typically started before it is set, so trusting only
    ``os.environ`` here would report a stale "unset" for the rest of the session.
    """
    raw = os.environ.get("OLLAMA_NUM_PARALLEL", "").strip()
    if not raw and os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                raw = str(winreg.QueryValueEx(key, "OLLAMA_NUM_PARALLEL")[0]).strip()
        except (OSError, ValueError, ImportError):
            raw = ""
    if not raw:
        return 0
    try:
        return max(0, min(int(raw), ABSOLUTE_MAX_WORKERS))
    except (TypeError, ValueError):
        return 0


def gpu_worker_slots() -> int:
    """Concurrent sequences the local GPU can batch, or 0 when unconstrained.

    Ollama keeps ONE copy of the model resident and batches concurrent
    requests against it, so the cost of another parallel worker is one more
    KV cache, not another set of weights. Returns 0 when there is no GPU or
    the model is unknown (CPU inference is bounded by ``cpu_slots`` instead).
    """
    total, _free = gpu_memory_bytes()
    if total <= 0:
        return 0
    model = fleet_model_bytes()
    if model <= 0:
        return 0
    headroom = total - model - GPU_RESERVE_BYTES
    if headroom <= 0:
        # The model only just fits: serialize rather than thrash/spill to CPU.
        return 1
    return max(1, min(ABSOLUTE_MAX_WORKERS, headroom // GPU_KV_CACHE_PER_WORKER_BYTES))


def capacity(requested_agents: int | str | None = None) -> dict:
    """Describe queued-agent ceiling separately from safe concurrent slots."""
    logical = max(1, int(os.cpu_count() or 1))
    ceiling = max_agents()
    requested = clamp_agent_count(
        requested_agents, default=ceiling if requested_agents is None else 3,
    )
    total, available = physical_memory_bytes()
    # Workers block on a network call to Ollama rather than burning a core
    # each, so half the logical CPUs is a sane ceiling (was logical // 4).
    cpu_slots = max(1, min(DEFAULT_MAX_WORKERS, logical // 2 or 1))
    if available > 0:
        usable = max(0, available - RAM_RESERVE_BYTES)
        ram_slots = max(1, usable // RAM_PER_WORKER_BYTES)
    else:
        ram_slots = DEFAULT_MAX_WORKERS
    gpu_slots = gpu_worker_slots()

    limits = {
        "requested": int(requested),
        "cpu": int(cpu_slots),
        "ram": int(ram_slots),
        "policy": int(DEFAULT_MAX_WORKERS),
    }
    if gpu_slots > 0:
        limits["gpu_vram"] = int(gpu_slots)
    # Ollama's own batching width is the hard ceiling on REAL concurrency --
    # handing it more concurrent requests than it will batch just queues them.
    ollama_parallel = ollama_parallel_limit()
    if ollama_parallel > 0:
        limits["ollama_num_parallel"] = int(ollama_parallel)
    automatic = max(1, min(limits.values()))
    bound_by = min(limits, key=lambda key: (limits[key], key))

    source = "auto"
    slots = automatic
    raw_override = os.environ.get("SONDER_PARALLEL_WORKERS", "").strip()
    if raw_override:
        try:
            override = int(raw_override)
        except (TypeError, ValueError):
            override = automatic
            source = "invalid override; auto"
        else:
            slots = max(1, min(override, requested, ABSOLUTE_MAX_WORKERS))
            source = "SONDER_PARALLEL_WORKERS"
            bound_by = "SONDER_PARALLEL_WORKERS"
    gpu_total, gpu_free = gpu_memory_bytes()
    warning = ""
    if ollama_parallel <= 0 and slots > 1:
        warning = (
            "OLLAMA_NUM_PARALLEL is unset -- Ollama auto-selects its batch width and "
            "collapses to 1 on a VRAM-tight box, which serializes every request and "
            "makes these %d worker slots buy nothing. Set OLLAMA_NUM_PARALLEL=%d and "
            "restart the Ollama server to actually get the concurrency."
            % (slots, slots)
        )
    elif 0 < ollama_parallel < automatic:
        warning = (
            "OLLAMA_NUM_PARALLEL=%d is below the %d slots this hardware could sustain; "
            "raise it (and restart Ollama) to use the remaining headroom."
            % (ollama_parallel, automatic)
        )
    return {
        "logical_cpus": logical,
        "total_memory_bytes": total,
        "available_memory_bytes": available,
        "gpu_total_memory_bytes": gpu_total,
        "gpu_free_memory_bytes": gpu_free,
        "fleet_model_bytes": fleet_model_bytes(),
        "ollama_num_parallel": int(ollama_parallel),
        "agent_ceiling": ceiling,
        "requested_agents": requested,
        "worker_slots": slots,
        "automatic_worker_slots": automatic,
        "slot_limits": limits,
        "bound_by": bound_by,
        "source": source,
        "warning": warning,
        "ram_reserve_bytes": RAM_RESERVE_BYTES,
        "ram_per_worker_bytes": RAM_PER_WORKER_BYTES,
    }


def parallel_worker_slots(requested_agents: int | str | None = None) -> int:
    return int(capacity(requested_agents)["worker_slots"])


def requests_fleet(task: str) -> bool:
    """Recognize explicit natural-language requests for maximum fan-out."""
    return bool(_FLEET_REQUEST.search(task or ""))


def requires_repository_tools(task: str) -> bool:
    """Return true when a task asks the model to inspect external repo state."""
    task = task or ""
    if _EMBEDDED_EVIDENCE.search(task):
        return False
    return bool(_REPOSITORY_REQUEST.search(task) or _REPO_FILE_PATH.search(task))


def evidence_gate(task: str, tools_available: bool = True) -> str:
    """Refuse ungrounded repo inspection only when guarded tools are unavailable."""
    if requires_repository_tools(task) and not tools_available:
        return EVIDENCE_REQUIRED
    return ""


def _repository_worker(prompt: str) -> str:
    """Lazily enter server's guarded agent loop without an import cycle."""
    import server

    result = server._agent_impl(
        prompt,
        tier="code",
        max_steps=8,
        allow_web=False,
        require_file_evidence=True,
        read_only=True,
        include_evidence=True,
        # auto_checklist is what arms the host's "use an inspection tool before
        # you finalize" nudge (and its retry). Without it this lane was
        # one-shot: a local model that answered on step 1 without touching a
        # file tool was immediately failed for missing file evidence, which is
        # exactly why delegated repository tasks always came back
        # EVIDENCE_REQUIRED. The direct agent surface passes it and works.
        auto_checklist=True,
        cancel_check=current_worker_cancel_requested,
    )
    if str(result or "").startswith("ERROR:"):
        raise RuntimeError(str(result)[:800])
    return result


def max_agents() -> int:
    """Configured upper bound for delegated subagents."""
    raw = os.environ.get("SONDER_MAX_AGENTS", str(hardware_max_agents()))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = hardware_max_agents()
    return max(1, min(value, ABSOLUTE_MAX_AGENTS))


def clamp_agent_count(count: int | str | None, default: int = 3) -> int:
    try:
        requested = int(count or default)
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, max_agents()))


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def estimate_tokens(text: str) -> int:
    text = text or ""
    return max(1, (len(text) + 3) // 4) if text else 0


def _event(agent_id: str, message: str) -> None:
    stamp = _stamp()
    with _LOCK:
        _EVENTS.append({
            "ts": stamp,
            "agent_id": agent_id,
            "message": message,
        })
        del _EVENTS[:-_MAX_EVENTS]
        row = dict(_AGENTS.get(agent_id) or {})
    try:
        if not row:
            row = fleet_store.get_agent(agent_id) or {}
        if row:
            fleet_store.add_event(
                agent_id, row.get("owner_id") or _OWNER_ID, stamp, message,
            )
    except Exception as exc:
        _remember_store_error(exc)


def _sync_local(row: dict | None) -> None:
    if not row:
        return
    with _LOCK:
        current = dict(row)
        current["updated_seq"] = next(_UPDATE_SEQUENCE)
        _AGENTS[current["id"]] = current


def _prune_local(finished_retention: int = 500) -> None:
    with _LOCK:
        finished = sorted(
            (
                row for row in _AGENTS.values()
                if row.get("status") not in ("queued", "running")
            ),
            key=lambda row: row.get("updated_ts") or 0,
            reverse=True,
        )
        for row in finished[max(10, int(finished_retention)):]:
            _AGENTS.pop(row["id"], None)


def _new_agent(
    role: str, task: str, parent_id: str = "", metadata: dict | None = None,
) -> str:
    _ensure_owner()
    metadata = dict(metadata or {})
    agent_id = "%s-%s" % (role, uuid.uuid4().hex[:12])
    now = _now()
    row = {
        "id": agent_id,
        "role": role,
        "parent_id": parent_id,
        "task": task,
        "status": "queued",
        "activity": "queued",
        "started_ts": now,
        "updated_ts": now,
        "finished_ts": None,
        "tool_calls": 0,
        "tokens_in": estimate_tokens(task),
        "tokens_out": 0,
        "files": [],
        "summary": "",
        "output": "",
        "error": "",
        "cancel_requested": False,
        "in_model_call": False,
        "requested_agents": int(metadata.get("requested_agents") or 0),
        "worker_slots": int(metadata.get("worker_slots") or 0),
        "mode": str(metadata.get("mode") or ""),
        "tier": str(metadata.get("tier") or ""),
        "retry_of": str(metadata.get("retry_of") or ""),
        "retried_by": "",
    }
    try:
        stored = fleet_store.create_agent(row, _OWNER_ID, os.getpid())
    except sqlite3.IntegrityError:
        # A 48-bit suffix collision is exceptionally unlikely; fail closed rather
        # than risk attaching work to another process's row.
        raise RuntimeError("fleet agent ID collision; retry orchestration")
    _sync_local(stored)
    if stored.get("cancel_requested"):
        _event(agent_id, "cancelled with parent before start")
    else:
        _event(agent_id, "queued: %s" % task[:140])
    return agent_id


def update_agent(agent_id: str, **changes) -> None:
    stored = fleet_store.update_agent(agent_id, _OWNER_ID, **changes)
    _sync_local(stored)
    if stored and "activity" in changes:
        _event(agent_id, stored.get("activity") or changes["activity"])


def cancel_requested(agent_id: str) -> bool:
    return fleet_store.cancellation_requested(agent_id)


def _start_agent(agent_id: str, activity: str, **changes) -> bool:
    """Atomically move a queued agent to running unless it was cancelled."""
    stored = fleet_store.start_agent(
        agent_id,
        _OWNER_ID,
        activity,
        in_model_call=bool(changes.get("in_model_call")),
        tool_calls=int(changes.get("tool_calls") or 0),
        requested_agents=int(changes.get("requested_agents") or 0),
        worker_slots=int(changes.get("worker_slots") or 0),
        mode=str(changes.get("mode") or ""),
        tier=str(changes.get("tier") or ""),
    )
    if not stored:
        return False
    _sync_local(stored)
    _event(agent_id, activity)
    return True


def _begin_model_call(agent_id: str, activity: str, tool_calls: int) -> bool:
    stored = fleet_store.begin_model_call(
        agent_id, _OWNER_ID, activity, tool_calls=tool_calls,
    )
    if not stored:
        return False
    _sync_local(stored)
    _event(agent_id, activity)
    return True


def request_cancel(selector: str) -> dict:
    """Request cooperative cancellation by exact ID, prefix, or ``all``."""
    result = fleet_store.cancel_agents(selector)
    for row in result.get("agents") or []:
        _sync_local(row)
        _event(row["id"], row.get("activity") or "cancellation requested")
    return result


def current_worker_cancel_requested() -> bool:
    """Whether the delegated worker bound to this thread was cancelled."""
    agent_id = getattr(_WORKER_LOCAL, "agent_id", None)
    return bool(agent_id and cancel_requested(agent_id))


@contextlib.contextmanager
def _bind_worker_agent(agent_id: str):
    previous_agent_id = getattr(_WORKER_LOCAL, "agent_id", None)
    _WORKER_LOCAL.agent_id = agent_id
    try:
        yield
    finally:
        _WORKER_LOCAL.agent_id = previous_agent_id


def _finish(agent_id: str, output: str = "", error: str = "") -> str:
    stored, final = fleet_store.finish_agent(
        agent_id, _OWNER_ID, output=output, error=error,
    )
    _sync_local(stored)
    if stored:
        _event(agent_id, stored.get("activity") or "finished")
        if stored.get("role") == "master":
            try:
                fleet_store.prune()
                _prune_local()
            except Exception as exc:
                _remember_store_error(exc)
    return final


def _run_worker(agent_id: str, prompt: str, worker_fn):
    if not _start_agent(
        agent_id, "calling model for delegated task", tool_calls=1,
        in_model_call=True,
    ):
        return "CANCELLED"
    with _bind_worker_agent(agent_id):
        try:
            output = worker_fn(prompt)
        except Exception as exc:  # defensive boundary for worker threads
            final = _finish(agent_id, error=str(exc))
            return final if final in ABORT_MARKERS else _WORKER_FAILED
    return _finish(agent_id, output=output)


def run_inline(task: str, worker_fn, metadata: dict | None = None) -> dict:
    if requires_repository_tools(task):
        worker_fn = _repository_worker
    metadata = dict(metadata or {})
    metadata.setdefault("mode", "inline")
    master_id = _new_agent("master", task, metadata=metadata)
    if not _start_agent(
        master_id, "running inline as master", tool_calls=1, in_model_call=True,
    ):
        return {"mode": "inline", "master_id": master_id, "output": "CANCELLED"}
    with _bind_worker_agent(master_id):
        try:
            output = worker_fn(task)
        except Exception as exc:
            final = _finish(master_id, error=str(exc))
            return {
                "mode": "inline",
                "master_id": master_id,
                "output": final if final in ABORT_MARKERS else "ERROR: %s" % exc,
            }
    final = _finish(master_id, output=output)
    return {"mode": "inline", "master_id": master_id, "output": final}


def _subtask_prompts(task: str, count: int, tool_access: bool = False) -> list[str]:
    count = clamp_agent_count(count, default=1)
    prompts = []
    for i in range(count):
        if tool_access:
            # A tool-equipped agent must be told to GO READ THE FILES. The
            # no-tools branch below tells the model to answer EVIDENCE_REQUIRED
            # when repository evidence is missing -- handing that same line to
            # an agent that actually HAS file tools primes it to bail out on
            # step 1 instead of calling them, and the host then rejects the
            # toolless answer (require_file_evidence), so the whole delegated
            # repository lane returned EVIDENCE_REQUIRED even on an authorized
            # root. Only claim the evidence is unreachable after the tools have
            # actually failed to reach it.
            access_contract = (
                "You have guarded read-only file tools. USE THEM: inspect the relevant "
                "allowed files with your file tools BEFORE answering -- an answer with "
                "no tool call is rejected by the host -- and never request "
                "write/edit/delete tools. Only if your file tools genuinely cannot reach "
                "the files (permission denied / not found after you have actually tried) "
                "answer EVIDENCE_REQUIRED and list the smallest missing inputs. "
            )
        else:
            access_contract = (
                "This is a greenfield design/implementation task, not a request to inspect "
                "an existing repository. You have no filesystem, shell, web, or hidden tool "
                "access; use the task as the specification and make explicit assumptions. "
                "If the task explicitly requires current repository evidence and it is "
                "absent, answer EVIDENCE_REQUIRED and list the smallest missing inputs. "
            )
        prompts.append(
            "You are delegated subagent %d/%d. %sNever "
            "claim that you inspected, edited, compiled, ran, or verified anything "
            "you were not explicitly shown. Quote the exact supporting excerpt for "
            "each codebase finding; label unsupported possibilities as hypotheses. "
            "For greenfield architecture, design, or implementation requests, make "
            "clearly labeled proposals from the task itself instead of refusing. "
            "Work independently and keep the answer concise."
            "\n\nTask:\n%s" % (i + 1, count, access_contract, task)
        )
    return prompts


def run_delegated(
    task: str, worker_fn, audit_fn, agents: int = 3,
    metadata: dict | None = None, _on_started=None,
) -> dict:
    if requires_repository_tools(task):
        worker_fn = _repository_worker
    agents = clamp_agent_count(agents, default=3)
    worker_slots = parallel_worker_slots(agents)
    metadata = dict(metadata or {})
    metadata.setdefault("mode", "delegated")
    metadata["requested_agents"] = agents
    metadata["worker_slots"] = worker_slots
    master_id = _new_agent("master", task, metadata=metadata)
    started = _start_agent(
        master_id,
        "queued %d agent(s) across %d worker slot(s)" % (agents, worker_slots),
        requested_agents=agents,
        worker_slots=worker_slots,
    )
    if not started:
        result = {
            "mode": "delegated",
            "master_id": master_id,
            "agents": [],
            "worker_slots": worker_slots,
            "outputs": [],
            "output": "CANCELLED",
        }
        if _on_started is not None:
            _on_started(dict(result))
        return result
    repository_task = requires_repository_tools(task)
    prompts = _subtask_prompts(task, agents, tool_access=repository_task)
    child_ids = [_new_agent("agent", prompt, parent_id=master_id) for prompt in prompts]
    if _on_started is not None:
        _on_started({
            "mode": "delegated",
            "master_id": master_id,
            "agents": list(child_ids),
            "worker_slots": worker_slots,
            "outputs": [],
            "output": "RUNNING",
        })
    outputs = []
    with ThreadPoolExecutor(max_workers=worker_slots) as pool:
        futures = {
            pool.submit(_run_worker, agent_id, prompt, worker_fn): agent_id
            for agent_id, prompt in zip(child_ids, prompts)
        }
        for future in as_completed(futures):
            agent_id = futures[future]
            try:
                output = future.result()
                if output not in ABORT_MARKERS and output is not _WORKER_FAILED:
                    outputs.append((agent_id, output))
            except Exception as exc:
                _finish(agent_id, error=str(exc))
    if cancel_requested(master_id):
        final = _finish(master_id)
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            "outputs": outputs,
            "output": final,
        }
    if not outputs:
        error = "all delegated workers failed before producing an auditable result"
        final = _finish(master_id, error=error)
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            "outputs": [],
            "output": final if final in ABORT_MARKERS else "ERROR: %s" % error,
        }
    if repository_task:
        outputs = [
            (agent_id, output)
            for agent_id, output in outputs
            if "=== TOOL EVIDENCE ===" in output
        ]
        if not outputs:
            merged = EVIDENCE_REQUIRED
            _finish(master_id, output=merged)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                "outputs": [],
                "output": merged,
            }
    if not _begin_model_call(
        master_id, "auditing delegated outputs", tool_calls=2,
    ):
        final = _finish(master_id)
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            "outputs": outputs,
            "output": final,
        }
    audit_prompt = [
        "You are the master orchestrator. You also have no filesystem or tool access. "
        "Audit the delegated outputs strictly against evidence quoted in the original "
        "task. Discard invented files, symbols, APIs, edits, test runs, and success "
        "claims. Never convert a proposal into a claim that work was completed. Resolve "
        "conflicts, separate verified findings from hypotheses. For repository tasks, "
        "end with an Evidence gaps section. For greenfield design/build tasks, "
        "implementation plans are valid outputs even when no repository evidence is "
        "provided. Return EVIDENCE_REQUIRED only when the original task explicitly "
        "requires current repository evidence and that evidence is unavailable. "
        "This task is greenfield because it did not ask to inspect an existing "
        "repository; therefore produce a concrete proposal/plan even without file "
        "evidence. For greenfield work, choose sensible defaults for unspecified "
        "libraries, mechanics, assets, and milestones; state those assumptions and "
        "turn them into implementation steps. Do not call ordinary design choices "
        "evidence gaps or ask the user to supply them. Honor explicit constraints "
        "such as no third-party libraries; if a platform API is needed, choose and "
        "name an in-house or OS-native alternative. End greenfield answers with "
        "Decisions made and Open risks, not an Evidence gaps questionnaire.",
        "",
        "Original task:",
        task,
        "",
    ]
    for agent_id, output in outputs:
        audit_prompt.extend(["--- %s ---" % agent_id, output, ""])
    with _bind_worker_agent(master_id):
        try:
            merged = audit_fn("\n".join(audit_prompt))
        except Exception as exc:
            merged = "ERROR: audit failed: %s" % exc
            final = _finish(master_id, error=str(exc))
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                "outputs": outputs,
                "output": final if final in ABORT_MARKERS else merged,
            }
    final = _finish(master_id, output=merged)
    return {
        "mode": "delegated",
        "master_id": master_id,
        "agents": child_ids,
        "worker_slots": worker_slots,
        "outputs": outputs,
        "output": final,
    }


def start_delegated(
    task: str, worker_fn, audit_fn, agents: int = 3,
    metadata: dict | None = None, startup_timeout: float = 5.0,
) -> dict:
    """Start delegated orchestration in a daemon thread and return ledger IDs.

    A hardware-width fleet can legitimately outlive an MCP request deadline.
    Running it synchronously made the initiating call time out and, on a
    single-request transport, prevented status and cancellation calls while the
    fleet was active. The durable fleet ledger remains authoritative after this
    function returns.
    """
    ready = threading.Event()
    started_result: dict = {}
    startup_error: list[BaseException] = []

    def on_started(result: dict) -> None:
        started_result.update(result)
        ready.set()

    def run() -> None:
        try:
            run_delegated(
                task,
                worker_fn=worker_fn,
                audit_fn=audit_fn,
                agents=agents,
                metadata=metadata,
                _on_started=on_started,
            )
        except Exception as exc:  # keep startup failures observable
            master_id = started_result.get("master_id")
            if master_id:
                with contextlib.suppress(Exception):
                    _finish(master_id, error=str(exc))
            else:
                startup_error.append(exc)
            ready.set()

    thread = threading.Thread(
        target=run,
        name="sonder-master-background",
        daemon=True,
    )
    thread.start()
    if not ready.wait(max(0.1, float(startup_timeout))):
        raise RuntimeError("background fleet did not initialize its durable ledger")
    if startup_error:
        raise RuntimeError("background fleet failed to start: %s" % startup_error[0])
    result = dict(started_result)
    result["background"] = True
    return result


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    try:
        data = fleet_store.snapshot(
            include_finished=include_finished, limit=limit,
        )
    except Exception as exc:
        _remember_store_error(exc)
        with _LOCK:
            rows = sorted(
                (dict(row) for row in _AGENTS.values()),
                key=lambda row: row.get("updated_seq") or 0,
                reverse=True,
            )
            active = [
                row for row in rows
                if row.get("status") in ("queued", "running")
            ]
            listed = rows if include_finished else active
            listed = listed[:max(1, int(limit or 20))]
            data = {
                "active_agents": len(active),
                "cancel_pending": sum(
                    1 for row in active if row.get("cancel_requested")
                ),
                "interrupted_agents": sum(
                    1 for row in rows if row.get("status") == "interrupted"
                ),
                "total_agents": len(rows),
                "total_listed": len(listed),
                "agents": listed,
                "events": list(_EVENTS[-_MAX_EVENTS:]),
                "tokens_in": sum(int(row.get("tokens_in") or 0) for row in rows),
                "tokens_out": sum(int(row.get("tokens_out") or 0) for row in rows),
                "latest_master_result": "",
                "database": "",
            }
    data["capacity"] = capacity()
    data["store_error"] = _STORE_ERROR
    return data


def recovery_candidate(selector: str) -> dict | None:
    """Resolve one persisted master by exact ID or unambiguous prefix."""
    return fleet_store.get_agent(selector, role="master")


def format_capacity(data: dict | None = None) -> str:
    data = data or capacity()
    gib = float(1024 ** 3)
    total = float(data.get("total_memory_bytes") or 0) / gib
    available = float(data.get("available_memory_bytes") or 0) / gib
    gpu_total = float(data.get("gpu_total_memory_bytes") or 0) / gib
    gpu_free = float(data.get("gpu_free_memory_bytes") or 0) / gib
    model = float(data.get("fleet_model_bytes") or 0) / gib
    limits = data.get("slot_limits") or {}
    lines = [
        "master orchestration capacity",
        "  logical CPUs: %s | RAM: %.1f/%.1f GiB available" % (
            data.get("logical_cpus", 0), available, total,
        ),
    ]
    if gpu_total > 0:
        # `model` is the fleet lane model's ON-DISK size (a static footprint used
        # for the VRAM headroom calc), NOT live residency -- labeling it
        # "resident" contradicted the live `free` reading (they could sum past
        # the card total, and it stayed constant while the GPU was actually idle).
        lines.append(
            "  GPU VRAM: %.1f/%.1f GiB free | fleet model: %.2f GiB on disk" % (
                gpu_free, gpu_total, model,
            )
        )
    else:
        lines.append("  GPU VRAM: not detected (CPU inference; bounded by CPU slots)")
    lines.extend([
        "  agent ceiling: %s queued | concurrent worker slots: %s" % (
            data.get("agent_ceiling", 0), data.get("worker_slots", 0),
        ),
        "  automatic slots: %s | bound by: %s | source: %s" % (
            data.get("automatic_worker_slots", 0),
            data.get("bound_by", "?"),
            data.get("source", "auto"),
        ),
    ])
    if limits:
        lines.append(
            "  per-constraint max: %s" % ", ".join(
                "%s=%s" % (name, limits[name]) for name in sorted(limits)
            )
        )
    parallel = int(data.get("ollama_num_parallel") or 0)
    lines.append(
        "  ollama batch width: %s" % (
            parallel if parallel > 0 else "unset (auto -- may serialize!)"
        )
    )
    lines.append(
        "  policy: reserve %.1f GiB RAM, %.2f GiB per worker; VRAM %.1f GiB reserved, "
        "%.2f GiB KV cache per parallel sequence" % (
            float(data.get("ram_reserve_bytes") or 0) / gib,
            float(data.get("ram_per_worker_bytes") or 0) / gib,
            GPU_RESERVE_BYTES / gib,
            GPU_KV_CACHE_PER_WORKER_BYTES / gib,
        )
    )
    warning = str(data.get("warning") or "").strip()
    if warning:
        lines.append("  WARNING: %s" % warning)
    return "\n".join(lines)


def format_snapshot(data: dict) -> str:
    lines = [
        "master orchestrator status",
        "  active agents: %s" % data.get("active_agents", 0),
        "  cancellation pending: %s" % data.get("cancel_pending", 0),
        "  interrupted/recoverable: %s" % data.get("interrupted_agents", 0),
        "  tokens in/out: %s/%s" % (data.get("tokens_in", 0), data.get("tokens_out", 0)),
    ]
    if data.get("database"):
        lines.append("  persistence: shared restart-safe fleet ledger")
    if data.get("store_error"):
        lines.append("  persistence warning: %s" % data["store_error"][:240])
    capacity_data = data.get("capacity") or {}
    if capacity_data:
        lines.append("  capacity: %s queued ceiling / %s active worker slot(s) [%s]" % (
            capacity_data.get("agent_ceiling", 0),
            capacity_data.get("worker_slots", 0),
            capacity_data.get("source", "auto"),
        ))
    agents = data.get("agents") or []
    if not agents:
        lines.append("  agents: none yet")
    for row in agents[:12]:
        lines.append("  - %(id)s [%(status)s] %(activity)s" % row)
        lines.append("      task: %s" % (row.get("task") or "")[:180])
    latest_result = data.get("latest_master_result") or ""
    if latest_result:
        lines.extend(["", "latest completed master result:", latest_result[:8000]])
    return "\n".join(lines)


def reset_for_tests() -> None:
    global _OWNER_REGISTERED, _STORE_ERROR
    with _LOCK:
        _AGENTS.clear()
        _EVENTS.clear()
    fleet_store.clear_all()
    _OWNER_REGISTERED = False
    _STORE_ERROR = ""

