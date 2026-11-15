"""Pure admission policy for the local GPU and the host memory behind it.

One loaded model costs more than VRAM: the runner process keeps weights, a
KV-cache mirror and compute buffers in host memory, and a Q8 9B model was seen
holding about 17 GB of it while 100% of the weights sat in VRAM. Admission that
only counts VRAM let the machine page until health reads took 26 s. Separately,
two callers asking the same loaded model for different ``num_ctx`` values make
Ollama reload it on every alternation.

This module decides, from values handed to it, and never reads memory, the
network, a clock, the environment or any store:

* which lane a request belongs to (``classify_operation``);
* which ``num_ctx`` a request is served at, given what is loaded or pinned
  (``resolve_num_ctx``);
* whether a cold load fits under the host-memory floors
  (``host_memory_verdict``) and what a model not yet measured is expected to
  cost (``estimate_host_footprint``, ``select_footprint``);
* the order waiters are served in (``queue_order``);
* whether a sent ``num_ctx`` matches the one the server reports
  (``check_effective_ctx``).

Every outcome is a typed value (``Admitted`` / ``Queued`` / ``Refused`` or an
enum). The enums, not message strings, are the signal callers branch on.
"""
from __future__ import annotations

import enum
import json
import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


class AdmissionLane(enum.Enum):
    """What a request does to the GPU, which decides the gate it passes."""

    PROBE = "probe"          # /api/show, /api/ps, /api/tags, /api/version
    LOAD = "load"            # warm-up: loads weights, generates nothing
    INFERENCE = "inference"  # chat / generate with content
    EMBED = "embed"          # /api/embeddings, /api/embed
    UNLOAD = "unload"        # keep_alive 0 with nothing to generate


class RefusalReason(enum.Enum):
    HOST_RAM_FLOOR = "host_ram_floor"
    COMMIT_FLOOR = "commit_floor"
    QUEUE_FULL = "queue_full"
    QUEUE_TIMEOUT = "queue_timeout"
    PROBE_BUSY = "probe_busy"
    CTX_PIN_CONFLICT = "ctx_pin_conflict"
    PROMPT_EXCEEDS_PINNED_CTX = "prompt_exceeds_pinned_ctx"
    STORE_UNAVAILABLE = "store_unavailable"
    DRAINING = "draining"


class CtxPolicy(enum.Enum):
    """How a request asking for a window other than the pinned one is handled."""

    SERVE_PINNED = "serve_pinned"  # rewrite num_ctx to the pin (logged once)
    WAIT_UNLOAD = "wait_unload"    # queue until the model unloads, else refuse
    EXACT = "exact"                # evidence is bound to the window: never rewrite


class CtxAction(enum.Enum):
    SERVE_AS_REQUESTED = "serve_as_requested"
    PIN_NEW = "pin_new"
    SERVE_PINNED = "serve_pinned"
    INHERIT_PINNED = "inherit_pinned"  # request named no window; use the pin
    REPIN = "repin"
    WAIT = "wait"
    REFUSE = "refuse"


class HostFloorMode(enum.Enum):
    QUEUE = "queue"
    REFUSE = "refuse"


class FootprintSource(enum.Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"


class CtxCheck(enum.Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class HostMemoryReading:
    """Host memory at one instant. ``commit_enforced`` is False on a kernel
    that overcommits (Linux ``vm.overcommit_memory`` other than 2), where a
    commit headroom number says nothing about whether an allocation succeeds."""

    available_physical_bytes: int
    total_physical_bytes: int = 0
    commit_limit_bytes: int = 0
    commit_available_bytes: int = 0
    commit_enforced: bool = True


@dataclass(frozen=True, slots=True)
class Footprint:
    """Host memory one loaded model is expected to hold in its runner."""

    working_set_bytes: int
    private_bytes: int
    source: FootprintSource
    digest: str = ""
    num_ctx: int = 0


@dataclass(frozen=True, slots=True)
class Admitted:
    lane: AdmissionLane
    num_ctx: int | None = None
    requested_num_ctx: int | None = None
    waited_seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Queued:
    reason: RefusalReason
    retry_after_seconds: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Refused:
    reason: RefusalReason
    detail: str = ""
    retry_after_seconds: float | None = None
    reading: HostMemoryReading | None = None
    footprint: Footprint | None = None
    evictable: tuple[str, ...] = ()


AdmissionDecision = Admitted | Queued | Refused


@dataclass(frozen=True, slots=True)
class CtxDecision:
    action: CtxAction
    num_ctx: int | None
    reason: RefusalReason | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    ticket_id: str
    priority_rank: int
    seq: int


@dataclass(frozen=True, slots=True)
class GpuAdmissionPolicy:
    """Validated knobs the pure functions consult."""

    min_available_ram_bytes: int = 4096 * 1024 * 1024
    min_commit_headroom_bytes: int = 4096 * 1024 * 1024
    on_host_floor: HostFloorMode = HostFloorMode.QUEUE
    ctx_conflict: CtxPolicy = CtxPolicy.SERVE_PINNED
    max_repins_per_model: int = 2
    repin_window_seconds: int = 600
    unmeasured_host_factor_pct: int = 125
    host_overhead_bytes: int = 1024 * 1024 * 1024
    chars_per_token: float = 3.5

    def __post_init__(self) -> None:
        for name in ("min_available_ram_bytes", "min_commit_headroom_bytes",
                     "max_repins_per_model", "host_overhead_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.repin_window_seconds) is not int or self.repin_window_seconds < 1:
            raise ValueError("repin_window_seconds must be a positive integer")
        if type(self.unmeasured_host_factor_pct) is not int or not 1 <= self.unmeasured_host_factor_pct <= 1000:
            raise ValueError("unmeasured_host_factor_pct must be within 1..1000")
        if not isinstance(self.on_host_floor, HostFloorMode):
            raise ValueError("on_host_floor must be a HostFloorMode")
        if self.ctx_conflict not in (CtxPolicy.SERVE_PINNED, CtxPolicy.WAIT_UNLOAD):
            raise ValueError("ctx_conflict must be serve_pinned or wait_unload")
        if not isinstance(self.chars_per_token, (int, float)) or not 0.5 <= float(self.chars_per_token) <= 16:
            raise ValueError("chars_per_token must be within 0.5..16")


_PROBE_PATHS = frozenset({"/api/show", "/api/ps", "/api/tags", "/api/version"})
_EMBED_PATHS = frozenset({"/api/embeddings", "/api/embed"})
_GENERATION_PATHS = frozenset({"/api/chat", "/api/generate"})
_ZERO_DURATION = re.compile(r"0+(\.0+)?(ns|us|ms|s|m|h)?", re.IGNORECASE)
_BACKGROUND_RANK = 1


def _path_only(path: str) -> str:
    return str(path or "").split("?", 1)[0].rstrip("/") or "/"


def is_zero_keep_alive(value: object) -> bool:
    """True for a keep_alive of zero in any form Ollama accepts (0, "0", "0s")."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    return isinstance(value, str) and _ZERO_DURATION.fullmatch(value.strip()) is not None


def _generates(path: str, payload: Mapping[str, object]) -> bool:
    """Whether a chat/generate request carries something to generate from."""
    if path == "/api/chat":
        messages = payload.get("messages")
        return isinstance(messages, (list, tuple)) and len(messages) > 0
    prompt = payload.get("prompt")
    images = payload.get("images")
    return (isinstance(prompt, str) and prompt != "") or (
        isinstance(images, (list, tuple)) and len(images) > 0)


def classify_operation(
    path: str, payload: Mapping[str, object] | None = None, *,
    method: str = "POST", prewarm: bool = False,
) -> AdmissionLane:
    """Lane for one Ollama request.

    Control-plane reads and unknown paths are PROBE: they never load a model,
    so they take no GPU slot. A keep_alive of zero is UNLOAD only when there is
    nothing to generate; a request with content and keep_alive 0 still loads the
    model and runs, so it stays INFERENCE.
    """
    clean = _path_only(path)
    body = payload if isinstance(payload, Mapping) else {}
    if str(method).upper() == "GET" or clean in _PROBE_PATHS:
        return AdmissionLane.PROBE
    if clean in _EMBED_PATHS:
        return AdmissionLane.UNLOAD if is_zero_keep_alive(body.get("keep_alive")) else AdmissionLane.EMBED
    if clean in _GENERATION_PATHS:
        generates = _generates(clean, body)
        if is_zero_keep_alive(body.get("keep_alive")) and not generates:
            return AdmissionLane.UNLOAD
        if prewarm or not generates:
            return AdmissionLane.LOAD
        return AdmissionLane.INFERENCE
    return AdmissionLane.PROBE


def priority_rank(caller: str) -> int:
    """Interactive work is served before agent, fleet and autopilot work."""
    return 0 if str(caller or "").strip().casefold() == "interactive" else _BACKGROUND_RANK


def queue_order(entries: Iterable[QueueEntry]) -> tuple[QueueEntry, ...]:
    """Waiters in service order: lower priority rank first, FIFO within a rank."""
    return tuple(sorted(entries, key=lambda entry: (entry.priority_rank, entry.seq)))


def requested_num_ctx(payload: Mapping[str, object] | None) -> int | None:
    """The positive integer ``options.num_ctx`` of a request, else None."""
    options = payload.get("options") if isinstance(payload, Mapping) else None
    value = options.get("num_ctx") if isinstance(options, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def requested_reserve_tokens(payload: Mapping[str, object] | None) -> int:
    """``options.num_predict`` as a token reserve; negative sentinels count as 0."""
    options = payload.get("options") if isinstance(payload, Mapping) else None
    value = options.get("num_predict") if isinstance(options, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def estimate_prompt_tokens(payload: Mapping[str, object] | None, chars_per_token: float = 3.5) -> int:
    """Conservative token count of what a request sends (characters / 3.5).

    A heuristic: it undercounts dense or non-English text, which is why a prompt
    that "fits" is checked against the pin with the caller's own num_predict
    reserved, and why a repin budget bounds the cost of being wrong.
    """
    if not isinstance(payload, Mapping):
        return 0
    chars = 0
    for key in ("prompt", "system", "input"):
        value = payload.get(key)
        if isinstance(value, str):
            chars += len(value)
        elif isinstance(value, (list, tuple)):
            chars += sum(len(item) for item in value if isinstance(item, str))
    messages = payload.get("messages")
    if isinstance(messages, (list, tuple)):
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, str):
                chars += len(content)
            elif content is not None:
                chars += len(json.dumps(content, default=str))
    tools = payload.get("tools")
    if tools:
        chars += len(json.dumps(tools, default=str))
    return int(math.ceil(chars / float(chars_per_token)))


def resolve_num_ctx(
    *, requested: int | None, current: int | None, policy: CtxPolicy,
    holders: int = 0, prompt_tokens: int = 0, reserve_tokens: int = 0,
    repins_in_window: int = 0, max_repins: int = 0,
) -> CtxDecision:
    """Decide the ``num_ctx`` a request is served at.

    ``current`` is the window the model is loaded (or pinned) at, None when it
    is not loaded. ``holders`` counts other requests holding the same model: a
    repin reloads the model and is only safe when nobody is using it.
    """
    if current is None:
        if requested is None:
            return CtxDecision(CtxAction.SERVE_AS_REQUESTED, None)
        return CtxDecision(CtxAction.PIN_NEW, requested)
    if requested is None:
        return CtxDecision(CtxAction.INHERIT_PINNED, current)
    if requested == current:
        return CtxDecision(CtxAction.SERVE_AS_REQUESTED, requested)
    detail = "requested %d, pinned %d" % (requested, current)
    if policy is not CtxPolicy.SERVE_PINNED:
        return CtxDecision(CtxAction.WAIT, None, RefusalReason.CTX_PIN_CONFLICT, detail)
    if prompt_tokens + reserve_tokens <= current:
        return CtxDecision(CtxAction.SERVE_PINNED, current, None, detail)
    if holders == 0 and repins_in_window < max_repins:
        return CtxDecision(CtxAction.REPIN, requested, None,
                           detail + "; prompt needs about %d tokens" % (prompt_tokens + reserve_tokens))
    return CtxDecision(
        CtxAction.REFUSE, None, RefusalReason.PROMPT_EXCEEDS_PINNED_CTX,
        detail + "; prompt needs about %d tokens" % (prompt_tokens + reserve_tokens),
    )


def estimate_host_footprint(
    file_size_bytes: int, *, factor_pct: int, overhead_bytes: int, kv_bytes: int = 0,
    digest: str = "", num_ctx: int = 0,
) -> Footprint:
    """Expected runner memory for a model never measured here.

    ``file_size * factor + KV + overhead``. The factor is an assumption, not a
    measurement; the first real load replaces it with the runner's own numbers.
    """
    size = max(0, int(file_size_bytes))
    total = size * int(factor_pct) // 100 + max(0, int(kv_bytes)) + max(0, int(overhead_bytes))
    return Footprint(total, total, FootprintSource.ESTIMATED, digest, int(num_ctx))


def select_footprint(
    *, measured_exact: Footprint | None, measured_other: Sequence[Footprint],
    file_size_bytes: int, requested_ctx: int, policy: GpuAdmissionPolicy, digest: str = "",
) -> Footprint:
    """Measured beats estimated; a measurement at another window beats a guess."""
    if measured_exact is not None:
        return measured_exact
    if measured_other:
        # The closest window is the best available proxy; ties take the larger
        # footprint so a wrong choice refuses too much rather than too little.
        return min(
            measured_other,
            key=lambda item: (abs(item.num_ctx - requested_ctx),
                              -(item.working_set_bytes + item.private_bytes)),
        )
    return estimate_host_footprint(
        file_size_bytes, factor_pct=policy.unmeasured_host_factor_pct,
        overhead_bytes=policy.host_overhead_bytes, digest=digest, num_ctx=requested_ctx,
    )


def host_memory_verdict(
    reading: HostMemoryReading, footprint: Footprint, *, policy: GpuAdmissionPolicy,
    lane: AdmissionLane = AdmissionLane.LOAD, evictable: Sequence[str] = (),
    retry_after_seconds: float = 1.0,
) -> AdmissionDecision:
    """Whether a cold load fits under the physical-RAM and commit floors.

    ``available - footprint.working_set >= min_available_ram`` and
    ``commit_available - footprint.private >= min_commit_headroom``. The commit
    check is skipped when the kernel does not enforce a commit limit. Under
    ``on_host_floor=queue`` a failing check is Queued (the caller re-measures);
    under ``refuse`` it is Refused at once, carrying the evidence.
    """
    reason: RefusalReason | None = None
    if reading.available_physical_bytes - footprint.working_set_bytes < policy.min_available_ram_bytes:
        reason = RefusalReason.HOST_RAM_FLOOR
    elif reading.commit_enforced and (
            reading.commit_available_bytes - footprint.private_bytes < policy.min_commit_headroom_bytes):
        reason = RefusalReason.COMMIT_FLOOR
    if reason is None:
        return Admitted(lane, detail="host memory fits")
    gib = 1024.0 ** 3
    detail = (
        "cold load needs about %.1f GiB working set and %.1f GiB private (%s); "
        "%.1f GiB RAM and %.1f GiB commit available, floors %.1f / %.1f GiB"
        % (footprint.working_set_bytes / gib, footprint.private_bytes / gib,
           footprint.source.value, reading.available_physical_bytes / gib,
           reading.commit_available_bytes / gib, policy.min_available_ram_bytes / gib,
           policy.min_commit_headroom_bytes / gib)
    )
    if policy.on_host_floor is HostFloorMode.QUEUE:
        return Queued(reason, retry_after_seconds, detail)
    return Refused(reason, detail, retry_after_seconds, reading, footprint, tuple(evictable))


def check_effective_ctx(sent: int | None, effective: int | None) -> CtxCheck:
    """Compare the window we sent with the one ``/api/ps`` reports as loaded."""
    if sent is None or effective is None:
        return CtxCheck.UNVERIFIABLE
    return CtxCheck.MATCH if sent == effective else CtxCheck.MISMATCH


__all__ = [
    "AdmissionDecision", "AdmissionLane", "Admitted", "CtxAction", "CtxCheck",
    "CtxDecision", "CtxPolicy", "Footprint", "FootprintSource", "GpuAdmissionPolicy",
    "HostFloorMode", "HostMemoryReading", "QueueEntry", "Queued", "Refused",
    "RefusalReason", "check_effective_ctx", "classify_operation",
    "estimate_host_footprint", "estimate_prompt_tokens", "host_memory_verdict",
    "is_zero_keep_alive", "priority_rank", "queue_order", "requested_num_ctx",
    "requested_reserve_tokens", "resolve_num_ctx", "select_footprint",
]
