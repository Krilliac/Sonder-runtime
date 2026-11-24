"""Master/subagent orchestration with live status snapshots."""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, wait

import sonder_runtime.adapters.execution.effect_fence as effect_fence
import sonder_runtime.adapters.execution.fleet_reservations as fleet_reservations
import sonder_runtime.adapters.fleet_creations as fleet_creations
import sonder_runtime.adapters.fleet_aggregation as fleet_aggregation
import sonder_runtime.adapters.agent_outcomes as agent_outcomes
import sonder_runtime.adapters.persistence.fleet_store as fleet_store
import sonder_runtime.domain.adaptive_concurrency as adaptive_concurrency
import sonder_runtime.domain.events as events
import sonder_runtime.domain.fleet_briefing as fleet_briefing
import sonder_runtime.domain.fleet_pressure as fleet_pressure
from sonder_runtime.application.artifacts import ArtifactReadiness
from sonder_runtime.application.orchestration import (
    capacity as _capacity_mod,
    execution as _execution_mod,
    scheduler as _scheduler_mod,
)
from sonder_runtime.application.orchestration.capacity import (
    ABORT_MARKERS,
    ABSOLUTE_MAX_AGENTS,
    ABSOLUTE_MAX_WORKERS,
    DEFAULT_MAX_AGENTS,
    DEFAULT_MAX_WORKERS,
    EVIDENCE_REQUIRED,
    GPU_KV_CACHE_PER_WORKER_BYTES,
    GPU_RESERVE_BYTES,
    RAM_PER_WORKER_BYTES,
    RAM_RESERVE_BYTES,
    REPOSITORY_EVIDENCE_TOOLS,
    STANDARD_MAX_WORKERS,
    TRANSIENT_FAILURE_CLASSES,
    estimate_tokens,
    evidence_gate,
    requested_worker_cap,
    requests_fleet,
    requires_repository_tools,
    worker_request_ignored_reason,
)
from sonder_runtime.application.orchestration.execution import (
    ReadyWorkerOutput,
    RepositoryWorkerResult,
)
from sonder_runtime.application.orchestration.scheduler import (
    AdaptiveLaneScheduler as _BaseAdaptiveLaneScheduler,
    FleetStalledError,
    bounded_error_text as _bounded_error_text,
    delegated_lane_claims,
    objective_assignments as _objective_assignments,
)
import fleet_provenance
from sonder_runtime.platform.runtime_threads import Thread


logger = logging.getLogger(__name__)


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
if "_RESERVED_SLOTS" not in globals():
    _RESERVED_SLOTS = 0
if "_SNAPSHOT_SUBSCRIBERS" not in globals():
    _SNAPSHOT_SUBSCRIBERS: list = []
if "_PRESSURE_TRACKER" not in globals():
    _PRESSURE_TRACKER = fleet_pressure.PressureTracker()
if "_OWNER_ID" not in globals():
    _OWNER_ID = "owner-%s-%s" % (os.getpid(), uuid.uuid4().hex[:12])
    _OWNER_STARTED_TS = time.time()
    _OWNER_REGISTERED = False
    _HEARTBEAT_THREAD = None
    _HEARTBEAT_STOP = threading.Event()
    _STORE_ERROR = ""
    _ATEXIT_REGISTERED = False
_reservation_state = fleet_reservations.ReservationLedger(globals())
_RESERVED_AGENT_IDS = globals()["_RESERVED_AGENT_IDS"]
if "_PRINCIPAL_ID" not in globals():
    _PRINCIPAL_ID = ""
    _PRINCIPAL_SECRET = ""


_MAX_EVENTS = 80
_FLEET_WORKER_CAP: int | None = None
_GPU_QUERY_CACHE_SECONDS = 15.0
HEARTBEAT_SECONDS = 5
_PROC_MEMINFO = "/proc/meminfo"

# Re-export compatibility names imported from orchestration modules.
ArtifactReadiness = ArtifactReadiness
RepositoryWorkerResult = RepositoryWorkerResult
ReadyWorkerOutput = ReadyWorkerOutput
FleetStalledError = FleetStalledError
EVIDENCE_REQUIRED = EVIDENCE_REQUIRED
ABORT_MARKERS = ABORT_MARKERS
DEFAULT_MAX_AGENTS = DEFAULT_MAX_AGENTS
ABSOLUTE_MAX_AGENTS = ABSOLUTE_MAX_AGENTS
DEFAULT_MAX_WORKERS = DEFAULT_MAX_WORKERS
STANDARD_MAX_WORKERS = STANDARD_MAX_WORKERS
ABSOLUTE_MAX_WORKERS = ABSOLUTE_MAX_WORKERS
RAM_RESERVE_BYTES = RAM_RESERVE_BYTES
RAM_PER_WORKER_BYTES = RAM_PER_WORKER_BYTES
GPU_RESERVE_BYTES = GPU_RESERVE_BYTES
GPU_KV_CACHE_PER_WORKER_BYTES = GPU_KV_CACHE_PER_WORKER_BYTES
REPOSITORY_EVIDENCE_TOOLS = REPOSITORY_EVIDENCE_TOOLS
TRANSIENT_FAILURE_CLASSES = TRANSIENT_FAILURE_CLASSES
estimate_tokens = estimate_tokens
evidence_gate = evidence_gate
requested_worker_cap = requested_worker_cap
requests_fleet = requests_fleet
requires_repository_tools = requires_repository_tools
worker_request_ignored_reason = worker_request_ignored_reason
delegated_lane_claims = delegated_lane_claims
_bounded_error_text = _bounded_error_text
_objective_assignments = _objective_assignments


def get_swarm_status() -> list[dict]:
    """Every local agent row, for the admin-only app-control swarm view."""
    with _LOCK:
        return [{"id": agent_id, **data} for agent_id, data in _AGENTS.items()]


def _lane_deadline(row, progress_deadline: float) -> float:
    """Progress deadline for one lane's current fleet row (see fleet_store)."""
    resolve = getattr(fleet_store, "lane_progress_deadline", None)
    if resolve is None:
        return float(progress_deadline)
    try:
        return float(resolve(row, progress_deadline))
    except Exception:
        return float(progress_deadline)


def configure_fleet_worker_cap(max_workers: int) -> None:
    global _FLEET_WORKER_CAP
    if max_workers < 1:
        raise ValueError("fleet_workers must be >= 1")
    _FLEET_WORKER_CAP = int(max_workers)


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


def _retained_messaging_available() -> bool:
    """Return whether the process-stable store has the new messaging API."""
    return all(
        callable(getattr(fleet_store, name, None))
        for name in (
            "local_principal_credentials",
            "register_principal",
            "list_agents_scoped",
            "queue_agent_message",
            "claim_agent_messages",
        )
    )


def _ensure_owner() -> None:
    global _OWNER_REGISTERED, _HEARTBEAT_THREAD, _ATEXIT_REGISTERED
    global _PRINCIPAL_ID, _PRINCIPAL_SECRET
    with _OWNER_SERVICE_LOCK:
        if not _retained_messaging_available():
            raise RuntimeError(
                "fleet orchestration requires a runtime restart after this update"
            )
        elif not _PRINCIPAL_ID or not _PRINCIPAL_SECRET:
            _PRINCIPAL_ID, _PRINCIPAL_SECRET = fleet_store.local_principal_credentials()
        else:
            fleet_store.register_principal(_PRINCIPAL_ID, _PRINCIPAL_SECRET)
        if not _OWNER_REGISTERED:
            fleet_store.register_owner(_OWNER_ID, os.getpid(), _OWNER_STARTED_TS)
            _OWNER_REGISTERED = True
        if _heartbeat_enabled() and (
            _HEARTBEAT_THREAD is None or not _HEARTBEAT_THREAD.is_alive()
        ):
            _HEARTBEAT_THREAD = Thread(
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
    """Return the local queued-candidate ceiling from logical CPU capacity."""
    logical = max(1, int(os.cpu_count() or 1))
    return max(DEFAULT_MAX_AGENTS, min(ABSOLUTE_MAX_AGENTS, logical * 2))


def _is_windows() -> bool:
    return os.name == "nt"


def _linux_meminfo_bytes() -> tuple[int, int] | None:
    """``(MemTotal, MemAvailable)`` from ``/proc/meminfo``, or ``None``."""
    try:
        with open(_PROC_MEMINFO, encoding="ascii", errors="replace") as handle:
            text = handle.read(64 * 1024)
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name in ("MemTotal", "MemAvailable") and fields:
            try:
                value = int(fields[0])
            except ValueError:
                return None
            unit = fields[1].lower() if len(fields) > 1 else ""
            values[name] = value * 1024 if unit == "kb" else value
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    total, available = values["MemTotal"], values["MemAvailable"]
    if total <= 0 or available < 0:
        return None
    return total, min(available, total)


def physical_memory_bytes() -> tuple[int, int]:
    """Return ``(total, available)`` physical RAM, or zeros if unavailable."""
    if not _is_windows():
        meminfo = _linux_meminfo_bytes()
        if meminfo is not None:
            return meminfo
    if _is_windows():
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
    """Return ``(total, free)`` VRAM of the largest local GPU, or zeros."""
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
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
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
    """On-disk size of the model the fleet lane runs, or 0 if unknown."""
    cached = globals().get("_FLEET_MODEL_CACHE")
    if cached and (time.time() - cached[0]) < _GPU_QUERY_CACHE_SECONDS:
        return cached[1]
    size = 0
    try:
        from sonder_runtime.bootstrap import delegation_wiring as _dw
        route = _dw.fleet_pool_route(sys.modules.get("server"))
        wanted = "" if route else _dw.fleet_model_name()
        size = int(route[0].model_size_bytes(route[1]) or 0) if route else 0
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
    """``OLLAMA_NUM_PARALLEL`` as the server would see it, or 0 when unset."""
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
    """Concurrent sequences the local GPU can batch, or 0 when unconstrained."""
    total, _free = gpu_memory_bytes()
    if total <= 0:
        return 0
    model = fleet_model_bytes()
    if model <= 0:
        return 0
    headroom = total - model - GPU_RESERVE_BYTES
    if headroom <= 0:
        return 1
    return max(1, min(ABSOLUTE_MAX_WORKERS, headroom // GPU_KV_CACHE_PER_WORKER_BYTES))


def _positive_int(value) -> tuple[int | None, str]:
    return _capacity_mod.positive_int(value)


def operator_worker_ceiling() -> tuple[int, str]:
    """Return the operator ceiling and any fail-safe configuration warning."""
    return _capacity_mod.parse_operator_worker_ceiling(
        os.environ.get("SONDER_MAX_WORKER_CAP", "")
    )


def explicit_agent_ceiling() -> int:
    """Breadth ceiling for a run that explicitly opts into a worker cap."""
    if os.environ.get("SONDER_MAX_AGENTS", "").strip():
        return max_agents()
    operator_ceiling, _error = operator_worker_ceiling()
    return max(1, min(ABSOLUTE_MAX_AGENTS, operator_ceiling))


def capacity(
    requested_agents: int | str | None = None,
    worker_cap: int | str | None = None,
) -> dict:
    """Describe queued breadth and bounded concurrent slots for one run."""
    logical = max(1, int(os.cpu_count() or 1))
    explicit_cap, cap_error = (
        _positive_int(worker_cap) if worker_cap is not None else (None, "")
    )
    ceiling = explicit_agent_ceiling() if explicit_cap else max_agents()
    if explicit_cap:
        requested_value, requested_error = _positive_int(requested_agents)
        requested = min(requested_value or explicit_cap, ceiling)
        if requested_error and requested_agents is not None:
            cap_error = "invalid requested_agents: %s" % requested_error
    else:
        requested = clamp_agent_count(
            requested_agents, default=ceiling if requested_agents is None else 3,
        )
    total, available = physical_memory_bytes()
    from sonder_runtime.bootstrap import delegation_wiring as _dw
    pool_route = _dw.fleet_pool_route(sys.modules.get("server"))
    gpu_slots = 0 if pool_route else gpu_worker_slots()
    ollama_parallel = (
        int(pool_route[2]["total"])
        if pool_route
        else _dw.effective_cluster_parallel(
            ollama_parallel_limit(), sys.modules.get("server")
        )
    )
    with _LOCK:
        reserved = _RESERVED_SLOTS
    operator_ceiling, operator_error = operator_worker_ceiling()
    gpu_total, gpu_free = gpu_memory_bytes()
    inputs = _capacity_mod.CapacityInputs(
        logical_cpus=logical,
        total_memory_bytes=total,
        available_memory_bytes=available,
        gpu_total_memory_bytes=gpu_total,
        gpu_free_memory_bytes=gpu_free,
        fleet_model_bytes=fleet_model_bytes(),
        gpu_slots=gpu_slots,
        ollama_parallel=ollama_parallel,
        pool_route_active=bool(pool_route),
        reserved_slots=reserved,
        fleet_worker_cap=_FLEET_WORKER_CAP,
        agent_ceiling=ceiling,
        requested_agents=requested,
        explicit_cap=explicit_cap,
        cap_error=cap_error,
        operator_ceiling=operator_ceiling,
        operator_error=operator_error,
        parallel_workers_raw=os.environ.get("SONDER_PARALLEL_WORKERS", ""),
        pool_note_resolver=lambda req, slots, bound: _dw.pool_capacity_note(
            pool_route, req, slots, bound
        ),
    )
    return _capacity_mod.compute_capacity(inputs)


def parallel_worker_slots(
    requested_agents: int | str | None = None,
    worker_cap: int | str | None = None,
) -> int:
    return int(capacity(requested_agents, worker_cap=worker_cap)["worker_slots"])


_REPOSITORY_ROOT_LABEL = re.compile(
    r"(?im)^\s*(?:repository|repo|project(?:\s+root)?)\s*:\s*(?P<value>[^\r\n]+)"
)


def repository_project_root(task: str) -> str:
    """Resolve an explicitly named repository/file to an existing project root."""
    text = str(task or "")
    for match in _REPOSITORY_ROOT_LABEL.finditer(text):
        raw = match.group("value").strip()
        if raw[:1] in {"\"", "'"}:
            quote = raw[0]
            close = raw.find(quote, 1)
            if close > 1:
                raw = raw[1:close]

        words = raw.split()
        candidates = [raw]
        candidates.extend(" ".join(words[:end]) for end in range(len(words) - 1, 0, -1))
        for value in candidates:
            candidate = value.strip().strip("\"'").rstrip(".,;:")
            if not candidate or not os.path.isabs(candidate):
                continue
            resolved = os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))
            if os.path.isdir(resolved):
                return resolved
            if os.path.isfile(resolved):
                return os.path.dirname(resolved)

    file_match = _capacity_mod._REPO_FILE_PATH.search(text)
    if file_match:
        candidate = file_match.group(0).rstrip(".,;:")
        if os.path.isabs(candidate):
            resolved = os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))
            if os.path.isfile(resolved):
                return os.path.dirname(resolved)
    return ""


def canonical_project_root(value: str) -> str:
    """Return one existing canonical directory, accepting a source-file path."""
    raw = str(value or "").strip().strip("\"'")
    if not raw or raw.lower() in {"none", "default"}:
        return ""
    try:
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    except (OSError, TypeError, ValueError):
        return ""
    if os.path.isfile(resolved):
        resolved = os.path.dirname(resolved)
    return resolved if os.path.isdir(resolved) else ""


def same_project_root(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _is_inside(path: str, root: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except (OSError, TypeError, ValueError):
        return False


def resolve_repository_project_root(task: str, project: str = "") -> str:
    """Resolve one unambiguous existing repository root or fail closed."""
    explicit_text = str(project or "").strip()
    explicit = canonical_project_root(explicit_text)
    inferred = repository_project_root(task)
    if explicit_text and not explicit:
        raise ValueError("project must name an existing directory or source file")
    if explicit:
        if inferred and not (
            same_project_root(inferred, explicit) or _is_inside(inferred, explicit)
        ):
            raise ValueError(
                "task repository path is outside the host-selected project root"
            )
        return explicit
    if inferred:
        from sonder_runtime.adapters.filesystem.file_ops import inside_allowed_roots
        if not inside_allowed_roots(inferred):
            raise ValueError(
                "task repository path is outside the configured file roots; "
                "add it to file_roots.local or SONDER_FILE_ROOTS, or select a project inside them"
            )
        return inferred
    raise ValueError(
        "repository work requires an explicit existing project root; no cwd fallback is allowed. "
        "Name it with a 'Repository: <absolute path>' line or select a project inside the configured file roots"
    )


def repository_worker_result(receipt, expected_project: str) -> RepositoryWorkerResult:
    """Validate a guarded host receipt before it can enter fleet aggregation."""
    if not hasattr(receipt, "project_scope"):
        raw = str(receipt or "")
        if raw.startswith("ERROR:"):
            raise RuntimeError(raw[:800])
    expected = canonical_project_root(expected_project)
    actual = canonical_project_root(getattr(receipt, "project_scope", ""))
    if not expected or not actual or not same_project_root(actual, expected):
        raise RuntimeError(
            "repository worker scope mismatch (expected=%r, actual=%r)"
            % (expected, actual)
        )
    tools = tuple(sorted(set(str(name) for name in (getattr(receipt, "tools", ()) or ()))))
    if not REPOSITORY_EVIDENCE_TOOLS.intersection(tools):
        raise agent_outcomes.no_evidence_error(receipt)
    output = str(getattr(receipt, "output", "") or "")
    if output.count("=== TOOL EVIDENCE ===") != 1:
        raise RuntimeError("repository worker omitted its guarded tool-evidence ledger")
    return RepositoryWorkerResult(
        output=output, project=expected, tools=tools,
        produced_files=tuple(getattr(receipt, "produced_files", ()) or ()),
        checks_run=bool(getattr(receipt, "validation_attempted", False)),
        checks_passed=getattr(receipt, "validation_passed", None),
    )


def _validate_repository_result(
    result, expected_project: str,
) -> RepositoryWorkerResult:
    if not isinstance(result, RepositoryWorkerResult):
        raise RuntimeError("repository worker returned no host-issued scope receipt")
    if not same_project_root(result.project, expected_project):
        raise RuntimeError("repository worker result escaped its assigned project scope")
    if not REPOSITORY_EVIDENCE_TOOLS.intersection(result.tools):
        raise RuntimeError("repository worker scope receipt has no file-evidence tool")
    if result.output.count("=== TOOL EVIDENCE ===") != 1:
        raise RuntimeError("repository worker scope receipt has no tool-evidence ledger")
    return result


def _render_repository_result(result: RepositoryWorkerResult) -> str:
    return fleet_aggregation.render_repository_result(result)


def _public_outputs(outputs) -> list[tuple[str, str]]:
    return [
        (
            agent_id,
            _render_repository_result(output)
            if isinstance(output, RepositoryWorkerResult) else str(output or ""),
        )
        for agent_id, output in outputs
    ]


def _repository_worker(prompt: str, project: str = "") -> RepositoryWorkerResult:
    """Lazily enter server's guarded agent loop without an import cycle."""
    import server

    project_scope = resolve_repository_project_root(prompt, project)
    receipt = server._agent_impl(
        prompt,
        tier="code",
        max_steps=8,
        allow_web=False,
        require_file_evidence=True,
        read_only=True,
        include_evidence=True,
        auto_checklist=True,
        project=project_scope,
        return_host_receipt=True,
        cancel_check=current_worker_cancel_requested,
    )
    return repository_worker_result(receipt, project_scope)


def max_agents() -> int:
    """Configured upper bound for delegated subagents."""
    raw = os.environ.get("SONDER_MAX_AGENTS", str(hardware_max_agents()))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = hardware_max_agents()
    return max(1, min(value, ABSOLUTE_MAX_AGENTS))


def clamp_agent_count(count: int | str | None, default: int = 3) -> int:
    """Clamp a positive explicit count, using ``default`` for zero/omitted input."""
    try:
        requested = int(count or default)
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, max_agents()))


def fleet_agent_count(agents=0) -> int:
    """Default queued breadth follows capacity, while explicit counts retain their cap."""
    requested = fleet_briefing.positive_count(agents)
    if requested:
        return clamp_agent_count(requested)
    return fleet_briefing.default_breadth(max_agents(), int(capacity()["worker_slots"]))


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


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


def subscribe_fleet_snapshots(callback) -> None:
    """Register a callback invoked on every fleet state transition."""
    with _LOCK:
        _SNAPSHOT_SUBSCRIBERS.append(callback)


def unsubscribe_fleet_snapshots(callback) -> None:
    with _LOCK:
        try:
            _SNAPSHOT_SUBSCRIBERS.remove(callback)
        except ValueError:
            pass


def _notify_snapshot_subscribers(agent_id: str, event: str) -> None:
    with _LOCK:
        subscribers = list(_SNAPSHOT_SUBSCRIBERS)
    if not subscribers:
        return
    try:
        snap = fleet_store.snapshot(include_finished=False, limit=ABSOLUTE_MAX_AGENTS + 1)
    except Exception as exc:
        _remember_store_error(exc)
        return
    _PRESSURE_TRACKER.update(
        snap.get("active_model_calls") or 0,
        snap.get("worker_slots_total") or max(1, snap.get("running_agents", 1)),
    )
    snap["pressure"] = {
        "ewma": _PRESSURE_TRACKER.ewma,
        "band": _PRESSURE_TRACKER.band,
    }
    for cb in subscribers:
        try:
            cb(snap, agent_id, event)
        except Exception:
            pass


def fleet_pressure_band() -> str:
    return _PRESSURE_TRACKER.band


def fleet_pressure_sample() -> fleet_pressure.PressureSample:
    return _PRESSURE_TRACKER.current()


def reserved_slot_count() -> int:
    with _LOCK:
        return _RESERVED_SLOTS


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
        "files": (
            [str(metadata.get("project"))]
            if metadata.get("project") else []
        ),
        "summary": "",
        "output": "",
        "error": "",
        "cancel_requested": False,
        "in_model_call": False,
        "requested_agents": int(metadata.get("requested_agents") or 0),
        "worker_slots": int(metadata.get("worker_slots") or 0),
        "mode": str(metadata.get("mode") or ""),
        "tier": str(metadata.get("tier") or ""),
        "project": str(metadata.get("project") or ""),
        "retry_of": str(metadata.get("retry_of") or ""),
        "retried_by": "",
        "master_task_digest": str(metadata.get("master_task_digest") or ""),
        "delegated_task_digest": str(metadata.get("delegated_task_digest") or ""),
        "objective_ids": list(metadata.get("objective_ids") or ()),
        "task_drift": False,
        "drift_metrics": {},
    }
    stored = _reservation_state.create(
        agent_id, fleet_store.create_agent, row, _OWNER_ID, os.getpid(),
        _PRINCIPAL_ID, _PRINCIPAL_SECRET,
    )
    _sync_local(stored)
    _reservation_state.release_cancelled(stored)
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
        with contextlib.suppress(Exception):
            _reservation_state.release_cancelled(fleet_store.get_agent(agent_id))
        return False
    _sync_local(stored)
    _event(agent_id, activity)
    _notify_snapshot_subscribers(agent_id, "start")
    with contextlib.suppress(Exception):
        events.emit(
            events.FLEET_AGENT_STARTED, "fleet", agent_id,
            {"task": (stored.get("task") or "")[:500],
             "mode": stored.get("mode") or ""},
        )
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
        _reservation_state.release_cancelled(row)
        _event(row["id"], row.get("activity") or "cancellation requested")
    return result


def current_worker_cancel_requested() -> bool:
    """Whether the worker bound to this thread (or, via its fence, this context) was cancelled."""
    agent_id = getattr(_WORKER_LOCAL, "agent_id", None) or effect_fence.current_fleet_agent_id()
    return bool(agent_id and cancel_requested(agent_id))


def active_model_call_count() -> int:
    """Return live fleet calls that still own an Ollama/HTTP request lane."""
    try:
        data = fleet_store.snapshot(
            include_finished=False, limit=ABSOLUTE_MAX_AGENTS + 1,
        )
        return int(data.get("active_model_calls") or 0)
    except Exception as exc:
        _remember_store_error(exc)
        return 1


def classify_worker_error(exc) -> str:
    """Map one worker exception onto the shared fanout failure vocabulary."""
    return _capacity_mod.classify_worker_error(
        exc,
        worker_model_failure_cls=agent_outcomes.WorkerModelFailure,
        timeout_types=(TimeoutError, subprocess.TimeoutExpired),
    )


def worker_transient_retries() -> int:
    """Bounded extra model-call attempts for transient delegated failures."""
    return _capacity_mod.parse_transient_retries(
        os.environ.get("SONDER_FLEET_TRANSIENT_RETRIES", "")
    )


@contextlib.contextmanager
def _bind_worker_agent(agent_id: str):
    """Bind the worker thread to its agent row, and fence its effects on it."""
    previous_agent_id = getattr(_WORKER_LOCAL, "agent_id", None)
    _WORKER_LOCAL.agent_id = agent_id
    try:
        with effect_fence.held(effect_fence.fleet_fence(agent_id, _OWNER_ID)):
            yield
    finally:
        _WORKER_LOCAL.agent_id = previous_agent_id


def _finish(
    agent_id: str,
    output: str = "",
    error: str = "",
    *,
    task_drift: bool = False,
    drift_metrics: dict | None = None,
) -> str:
    stored, final = fleet_store.finish_agent(
        agent_id,
        _OWNER_ID,
        output=output,
        error=error,
        task_drift=task_drift,
        drift_metrics=drift_metrics,
    )
    _sync_local(stored)
    _reservation_state.release(agent_id)
    if stored:
        _event(agent_id, stored.get("activity") or "finished")
        if stored.get("role") == "master":
            try:
                fleet_store.prune()
                _prune_local()
            except Exception as exc:
                _remember_store_error(exc)
    _notify_snapshot_subscribers(agent_id, "finish")
    with contextlib.suppress(Exception):
        _duration = (
            (stored.get("finished_ts") or 0) - (stored.get("started_ts") or 0)
            if stored else 0
        )
        if stored and stored.get("status") in ("failed", "task_drift"):
            events.emit(
                events.FLEET_AGENT_FAILED, "fleet", agent_id,
                {"error": str(stored.get("error") or "")},
            )
        elif stored:
            events.emit(
                events.FLEET_AGENT_DONE, "fleet", agent_id,
                {"result": (stored.get("output") or "")[:500], "duration": _duration},
            )
    return final


def _readiness_verifier_receipt(
    agent_id: str, run_id: str, delegated_task_digest: str,
    output: str, verification: dict,
) -> str:
    return _execution_mod.readiness_verifier_receipt(
        agent_id, run_id, delegated_task_digest, output, verification,
        task_digest_fn=fleet_provenance.task_digest,
    )


def _run_worker(
    agent_id: str,
    prompt: str,
    worker_fn,
    project_scope: str = "",
    master_task: str = "",
    objectives=(),
    master_task_digest: str = "",
    delegated_task_digest: str = "",
    run_id: str = "",
    outcome_sink=None,
    strategy_observer=None,
    model_route: str = "",
    creation_worker_root: str = "",
):
    hooks = _execution_mod.WorkerExecutionHooks(
        start_agent=_start_agent,
        begin_model_call=_begin_model_call,
        finish=_finish,
        event=_event,
        bind_worker_agent=_bind_worker_agent,
        cancel_requested=cancel_requested,
        validate_delegation=fleet_provenance.validate_delegation,
        validate_result=fleet_provenance.validate_result,
        validate_repository_result=_validate_repository_result,
        render_repository_result=_render_repository_result,
        attach_creation_receipt=fleet_creations.attach_receipt,
        classify_worker_error=classify_worker_error,
        retry_plan=lambda exc, attempt, allowed, cls, classes, is_cancelled: agent_outcomes.retry_plan(
            exc, attempt, allowed, cls, classes, fleet_store, is_cancelled,
        ),
        retry_pause=agent_outcomes.retry_pause,
        remember_transient_failure=agent_outcomes.remember_transient_failure,
        worker_transient_retries=worker_transient_retries,
        task_digest=fleet_provenance.task_digest,
        worker_failed_sentinel=_WORKER_FAILED,
    )
    return _execution_mod.execute_worker(
        agent_id,
        prompt,
        worker_fn,
        hooks=hooks,
        project_scope=project_scope,
        master_task=master_task,
        objectives=objectives,
        master_task_digest=master_task_digest,
        delegated_task_digest=delegated_task_digest,
        run_id=run_id,
        outcome_sink=outcome_sink,
        strategy_observer=strategy_observer,
        model_route=model_route,
        creation_worker_root=creation_worker_root,
    )


def run_inline(
    task: str, worker_fn, metadata: dict | None = None, project: str = "",
) -> dict:
    hooks = _execution_mod.InlineExecutionHooks(
        parse_objectives=fleet_provenance.parse_objectives,
        resolve_repository_project_root=resolve_repository_project_root,
        task_digest=fleet_provenance.task_digest,
        objective_contract=fleet_provenance.objective_contract,
        new_agent=_new_agent,
        start_agent=_start_agent,
        begin_model_call=_begin_model_call,
        finish=_finish,
        bind_worker_agent=_bind_worker_agent,
        validate_delegation=fleet_provenance.validate_delegation,
        validate_result=fleet_provenance.validate_result,
        validate_repository_result=_validate_repository_result,
        render_repository_result=_render_repository_result,
    )
    return _execution_mod.execute_inline(
        task, worker_fn, hooks=hooks, metadata=metadata, project=project,
    )


def _subtask_prompts(
    task: str,
    count: int,
    tool_access: bool = False,
    project: str = "",
    objective_assignments=(),
    build_projects=(),
) -> list[str]:
    count = clamp_agent_count(count, default=1)
    return fleet_briefing.subtask_prompts(
        task, count, tool_access=tool_access,
        project=project,
        build_projects=tuple(build_projects),
        objective_contracts=tuple(
            fleet_provenance.objective_contract(assigned)
            for assigned in objective_assignments[:count]
        ),
        protected=bool(objective_assignments) or "[objective:" in task.lower(),
    )


def adaptive_concurrency_enabled() -> bool:
    """Operator rollback switch for adaptive fleet concurrency."""
    raw = os.environ.get("SONDER_FLEET_ADAPTIVE_CONCURRENCY", "")
    enabled, invalid = _capacity_mod.parse_adaptive_concurrency_flag(raw)
    if invalid:
        logger.warning(
            "ignoring unrecognized SONDER_FLEET_ADAPTIVE_CONCURRENCY=%r; "
            "expected one of 0/false/no/off or 1/true/yes/on; adaptive stays on",
            raw.strip().lower()[:32],
        )
    return enabled


def concurrency_resources() -> adaptive_concurrency.ResourceSnapshot:
    """Measure host memory headroom for the adaptive lane scheduler."""
    total, available = physical_memory_bytes()
    if total <= 0 or available <= 0:
        return adaptive_concurrency.ResourceSnapshot()
    return adaptive_concurrency.ResourceSnapshot(
        memory_available_fraction=min(1.0, available / total),
    )


class AdaptiveLaneScheduler(_BaseAdaptiveLaneScheduler):
    """Master-orchestrator scheduler bound to live host memory snapshots."""

    def __init__(
        self,
        claims,
        ceiling: int,
        *,
        adaptive: bool = True,
        resources=None,
        policy: adaptive_concurrency.ConcurrencyPolicy | None = None,
        on_decision=None,
    ) -> None:
        super().__init__(
            claims,
            ceiling,
            adaptive=adaptive,
            resources=resources or concurrency_resources,
            policy=policy,
            on_decision=on_decision,
        )


def dispatch_lanes(
    scheduler, max_workers: int, run_lane, collect, on_error, on_abandon=None,
    on_stall=None,
) -> None:
    """Run every scheduled lane, admitting only what ``scheduler`` allows."""
    _scheduler_mod.dispatch_lanes(
        scheduler,
        max_workers,
        run_lane,
        collect,
        on_error,
        on_abandon=on_abandon,
        on_stall=on_stall,
        executor_cls=ThreadPoolExecutor,
        wait_fn=wait,
        get_agent_fn=fleet_store.get_agent,
        progress_deadline_fn=lambda: float(
            getattr(fleet_store, "progress_deadline_seconds", lambda: 120.0)()
        ),
        lane_deadline_fn=_lane_deadline,
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )


def run_delegated(
    task: str, worker_fn, audit_fn, agents: int = 3,
    metadata: dict | None = None, _on_started=None, project: str = "",
    worker_cap: int | str | None = None,
    strategy_observer=None,
    build_workspace: bool = False,
) -> dict:
    hooks = _execution_mod.DelegatedExecutionHooks(
        parse_objectives=fleet_provenance.parse_objectives,
        resolve_repository_project_root=resolve_repository_project_root,
        same_project_root=same_project_root,
        explicit_agent_ceiling=explicit_agent_ceiling,
        clamp_agent_count=clamp_agent_count,
        capacity=capacity,
        parallel_worker_slots=parallel_worker_slots,
        task_digest=fleet_provenance.task_digest,
        objective_contract=fleet_provenance.objective_contract,
        subtask_prompts=_subtask_prompts,
        new_agent=_new_agent,
        start_agent=_start_agent,
        begin_model_call=_begin_model_call,
        finish=_finish,
        event=_event,
        request_cancel=request_cancel,
        cancel_requested=cancel_requested,
        release_reservation=_reservation_state.release,
        bind_worker_agent=_bind_worker_agent,
        run_worker=_run_worker,
        dispatch_lanes=dispatch_lanes,
        scheduler_cls=AdaptiveLaneScheduler,
        adaptive_concurrency_enabled=adaptive_concurrency_enabled,
        render_repository_result=_render_repository_result,
        public_outputs=_public_outputs,
        validate_result=fleet_provenance.validate_result,
        aggregation_metrics=fleet_provenance.aggregation_metrics,
        validate_aggregate_output=fleet_provenance.validate_aggregate_output,
        capacity_unavailable_error=agent_outcomes.capacity_unavailable_error,
        create_workspace=fleet_creations.create_workspace,
        release_workspace=fleet_creations.release_workspace,
        build_report=fleet_aggregation.build_report,
        audit_prompt=fleet_aggregation.audit_prompt,
        worker_failed_sentinel=_WORKER_FAILED,
    )
    return _execution_mod.execute_delegated(
        task,
        worker_fn,
        audit_fn,
        hooks=hooks,
        agents=agents,
        metadata=metadata,
        on_started=_on_started,
        project=project,
        worker_cap=worker_cap,
        strategy_observer=strategy_observer,
        build_workspace=build_workspace,
    )


def start_delegated(
    task: str, worker_fn, audit_fn, agents: int = 3,
    metadata: dict | None = None, startup_timeout: float = 5.0,
    project: str = "", worker_cap: int | str | None = None,
    strategy_observer=None,
    build_workspace: bool = False,
) -> dict:
    """Start delegated orchestration in a daemon thread and return ledger IDs."""
    return _execution_mod.start_delegated_background(
        run_delegated_fn=run_delegated,
        request_cancel_fn=request_cancel,
        finish_fn=_finish,
        thread_cls=Thread,
        task=task,
        worker_fn=worker_fn,
        audit_fn=audit_fn,
        agents=agents,
        metadata=metadata,
        startup_timeout=startup_timeout,
        project=project,
        worker_cap=worker_cap,
        strategy_observer=strategy_observer,
        build_workspace=build_workspace,
    )


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    try:
        data = fleet_store.snapshot(
            include_finished=include_finished, limit=limit,
        )
    except Exception as exc:
        _remember_store_error(exc)
        with _LOCK:
            data = _capacity_mod.build_fallback_snapshot(
                _AGENTS,
                _EVENTS,
                include_finished=include_finished,
                limit=limit,
                max_events=_MAX_EVENTS,
                progress_deadline_seconds=float(
                    getattr(fleet_store, "progress_deadline_seconds", lambda: 120.0)()
                ),
            )
    from sonder_runtime.application.agents.delegation_runtime import merge_ephemeral_snapshot
    merge_ephemeral_snapshot(data, include_finished=include_finished, limit=limit)
    data["capacity"] = capacity()
    data["store_error"] = _STORE_ERROR
    return data


def recovery_candidate(selector: str) -> dict | None:
    """Resolve one persisted master by exact ID or unambiguous prefix."""
    return fleet_store.get_agent(selector, role="master")


def retry_fence_available() -> bool:
    """Whether the process-stable store carries the retry idempotency fence."""
    return all(
        callable(getattr(fleet_store, name, None))
        for name in ("acquire_retry_lease", "release_retry_lease")
    )


def acquire_retry_lease(agent_id: str) -> dict | None:
    """Claim one retry dispatch slot for a persisted master, or ``None``."""
    if not retry_fence_available():
        return None
    return fleet_store.acquire_retry_lease(agent_id)


def release_retry_lease(agent_id: str, token: str) -> bool:
    """Release a pending retry dispatch claim after dispatch or on error."""
    if not retry_fence_available():
        return False
    return fleet_store.release_retry_lease(agent_id, token)


def discover_retained_agents(
    *, project: str = "", parent_id: str = "", include_finished: bool = True,
    limit: int = 50,
) -> list[dict]:
    """Discover agents owned by this runtime inside one explicit scope."""
    _ensure_owner()
    if not _retained_messaging_available():
        raise RuntimeError(
            "retained agent messaging requires a runtime restart after this update"
        )
    scoped_project = canonical_project_root(project) if project else ""
    if project and not scoped_project:
        raise ValueError("project must name an existing directory or source file")
    return fleet_store.list_agents_scoped(
        _OWNER_ID,
        project=scoped_project,
        parent_id=parent_id,
        include_finished=include_finished,
        limit=limit,
        principal_id=_PRINCIPAL_ID,
        principal_secret=_PRINCIPAL_SECRET,
    )


def send_retained_agent_message(
    sender_id: str, recipient_id: str, message: str, *, mode: str,
    project: str = "",
) -> dict:
    """Queue a same-principal, same-project, same-tree follow-up or steer."""
    _ensure_owner()
    if not _retained_messaging_available():
        raise RuntimeError(
            "retained agent messaging requires a runtime restart after this update"
        )
    scoped_project = canonical_project_root(project) if project else ""
    if project and not scoped_project:
        raise ValueError("project must name an existing directory or source file")
    receipt = fleet_store.queue_agent_message(
        sender_id,
        recipient_id,
        _OWNER_ID,
        project=scoped_project,
        mode=mode,
        body=message,
        principal_id=_PRINCIPAL_ID,
        principal_secret=_PRINCIPAL_SECRET,
    )
    _event(recipient_id, "%s message queued [%s]" % (receipt["mode"], receipt["message_id"]))
    return receipt


def receive_retained_agent_messages(
    recipient_id: str, *, project: str = "", limit: int = 8,
) -> list[dict]:
    """Claim messages at a trusted cooperative worker checkpoint."""
    _ensure_owner()
    if not _retained_messaging_available():
        raise RuntimeError(
            "retained agent messaging requires a runtime restart after this update"
        )
    scoped_project = canonical_project_root(project) if project else ""
    if project and not scoped_project:
        raise ValueError("project must name an existing directory or source file")
    receipts = fleet_store.claim_agent_messages(
        recipient_id, _OWNER_ID, project=scoped_project, limit=limit,
        principal_id=_PRINCIPAL_ID, principal_secret=_PRINCIPAL_SECRET,
    )
    for receipt in receipts:
        _event(
            recipient_id,
            "%s message delivered [%s]" % (
                receipt["mode"], receipt["message_id"],
            ),
        )
    return receipts


def format_capacity(data: dict | None = None) -> str:
    return _capacity_mod.format_capacity(data or capacity())


def format_snapshot(data: dict) -> str:
    pending_prefix = getattr(fleet_store, "RETRY_LEASE_PREFIX", "retry-pending:")
    return _capacity_mod.format_snapshot(data, retry_lease_prefix=pending_prefix)


def reset_for_tests() -> None:
    global _OWNER_REGISTERED, _STORE_ERROR, _RESERVED_SLOTS
    with _LOCK:
        _AGENTS.clear()
        _EVENTS.clear()
        _RESERVED_AGENT_IDS.clear()
        _RESERVED_SLOTS = 0
    fleet_store.clear_all()
    from sonder_runtime.application.agents.delegation_runtime import clear_ephemeral_state
    clear_ephemeral_state()
    _OWNER_REGISTERED = False
    _STORE_ERROR = ""
