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
    MANAGE = "manage"        # /api/pull, /api/create, /api/delete ...: not gated


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
    STREAMED = "streamed"  # experts live on disk / CPU: dense + KV + expert cache


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
    streamed_workspace_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("min_available_ram_bytes", "min_commit_headroom_bytes",
                     "max_repins_per_model", "host_overhead_bytes",
                     "streamed_workspace_bytes"):
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
_API = "/api/"
_CHAT_PATH = _API + "chat"
_GENERATION_PATHS = frozenset({_CHAT_PATH, _API + "generate"})
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
    if path == _CHAT_PATH:
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

    Control-plane reads (GET, /api/show) are PROBE: short, never load a model,
    no GPU slot. Any other POST (/api/pull, /api/create, /api/copy, /api/delete,
    /api/push) is MANAGE: it neither loads nor runs a model but can take minutes,
    so it holds neither a probe slot nor a GPU slot. A keep_alive of zero is
    UNLOAD only when there is nothing to generate; a request with content and
    keep_alive 0 still loads the model and runs, so it stays INFERENCE.
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
    return AdmissionLane.MANAGE


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


_DEFAULT_VOCAB = 160_000  # assumed when metadata names none: over-counts dense, never under
_KV_ELEMENT_BYTES = 2     # f16 K and V
_DEFAULT_WINDOW = 4096    # a server's usual default window, when nothing else names one


@dataclass(frozen=True, slots=True)
class StreamedPlacement:
    """What a binding declares about a model whose experts stream from disk.

    ``expert_cache_bytes`` is the host memory the backend is configured to keep
    expert tensors in; the rest of the experts is file-backed and not resident.
    ``dense_bytes`` and ``kv_bytes_per_token`` override what would be derived
    from the model's metadata; ``state_bytes`` is fixed recurrent state
    (linear-attention or SSM layers) that does not grow with the window.
    ``num_ctx`` is the window the backend serves when a request names none.
    """

    expert_cache_bytes: int
    dense_bytes: int | None = None
    kv_bytes_per_token: int | None = None
    state_bytes: int = 0
    num_ctx: int | None = None

    def __post_init__(self) -> None:
        for name in ("expert_cache_bytes", "state_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("dense_bytes", "kv_bytes_per_token"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be None or a non-negative integer")
        if self.num_ctx is not None and (type(self.num_ctx) is not int or self.num_ctx < 1):
            raise ValueError("num_ctx must be None or a positive integer")


@dataclass(frozen=True, slots=True)
class ExpertModelShape:
    """The architecture numbers of a mixture-of-experts model (GGUF metadata)."""

    block_count: int
    embedding_length: int
    head_count: int
    head_count_kv: int
    key_length: int
    value_length: int
    expert_count: int
    expert_used_count: int
    expert_ff_length: int
    shared_expert_count: int = 0
    shared_expert_ff_length: int = 0
    leading_dense_blocks: int = 0
    ff_length: int = 0
    vocab_size: int = 0
    context_length: int = 0


def _info_int(info: Mapping[str, object], arch: str, suffix: str) -> int:
    """A positive int (the largest of a per-layer list) at ``<arch>.<suffix>``, else 0.

    Only when ``arch`` is empty does any key ending in ``.<suffix>`` stand in, so a
    sibling block such as ``clip.vision.attention.head_count`` is never read."""
    if arch:
        keys = [f"{arch}.{suffix}"]
    else:
        keys = sorted(key for key in info if key.endswith("." + suffix))
    for key in keys:
        value = info.get(key)
        if isinstance(value, (list, tuple)):
            numbers = [v for v in value if isinstance(v, int) and not isinstance(v, bool)]
            value = max(numbers) if numbers else 0
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


def expert_model_shape(model_info: Mapping[str, object] | None) -> ExpertModelShape | None:
    """Parse ``/api/show`` ``model_info``; None unless it describes an expert model
    with enough numbers to size attention and KV (blocks, width, heads, experts)."""
    if not isinstance(model_info, Mapping):
        return None
    arch = str(model_info.get("general.architecture") or "")

    def get(suffix: str) -> int:
        return _info_int(model_info, arch, suffix)

    blocks, width, heads = get("block_count"), get("embedding_length"), get("attention.head_count")
    experts = get("expert_count")
    if not (blocks and width and heads and experts > 1):
        return None
    kv_heads = get("attention.head_count_kv") or heads
    key_length = get("attention.key_length") or width // heads
    value_length = get("attention.value_length") or key_length
    return ExpertModelShape(
        blocks, width, heads, kv_heads, key_length, value_length, experts,
        get("expert_used_count"), get("expert_feed_forward_length"),
        get("expert_shared_count"), get("expert_shared_feed_forward_length"),
        min(get("leading_dense_block_count"), blocks), get("feed_forward_length"),
        get("vocab_size"), get("context_length"))


def kv_bytes_per_token(shape: ExpertModelShape) -> int:
    """f16 K and V bytes one token adds across every block.

    Multi-head latent attention and sliding-window models need less than this, so
    they are over-counted, which is the fail-safe direction."""
    return shape.block_count * shape.head_count_kv * (
        shape.key_length + shape.value_length) * _KV_ELEMENT_BYTES


def _expert_and_dense_params(shape: ExpertModelShape) -> tuple[int, int]:
    """(expert parameters, everything-else parameters), a structural estimate.

    Experts: three ``width x ff`` matrices each, on the non-leading blocks.
    Dense: attention projections, router, shared experts, leading dense FFNs and
    input/output embeddings (a large vocabulary is assumed when none is named,
    so the dense share is over- rather than under-counted).
    """
    width = shape.embedding_length
    moe_blocks = shape.block_count - shape.leading_dense_blocks
    experts = moe_blocks * shape.expert_count * 3 * width * shape.expert_ff_length
    attention = shape.block_count * width * (
        2 * shape.head_count * shape.key_length
        + shape.head_count_kv * (shape.key_length + shape.value_length))
    router = moe_blocks * width * shape.expert_count
    shared = moe_blocks * shape.shared_expert_count * 3 * width * (
        shape.shared_expert_ff_length or shape.expert_ff_length)
    leading = shape.leading_dense_blocks * 3 * width * shape.ff_length
    embeddings = 2 * (shape.vocab_size or _DEFAULT_VOCAB) * width
    return experts, attention + router + shared + leading + embeddings


def estimate_streamed_footprint(
    *, file_size_bytes: int, num_ctx: int, placement: StreamedPlacement,
    shape: ExpertModelShape | None, policy: GpuAdmissionPolicy, digest: str = "",
) -> Footprint | None:
    """Resident host memory of an expert-streaming model, or None if undecidable.

    ``dense + KV/state + workspace + expert cache + reserve``:

    * dense: ``placement.dense_bytes``, else the file size times the dense share
      of the structural parameter count (``_expert_and_dense_params``);
    * KV: ``placement.kv_bytes_per_token`` (else f16 K+V from ``shape``) times
      the window, plus ``placement.state_bytes``. A request that names no window
      (``num_ctx`` of 0) is charged ``placement.num_ctx``, else the model's own
      ``context_length``, else ``_DEFAULT_WINDOW``: the server loads it at some
      window, and this estimate is too tight to leave that uncounted;
    * expert cache: configured, clamped to the expert bytes that exist;
    * workspace and reserve: ``policy.streamed_workspace_bytes`` and
      ``policy.host_overhead_bytes``.

    Working set and private bytes are both the resident total: the cache is
    anonymous memory and file-backed expert pages are reclaimable, so neither
    the file size nor ``size * 1.25`` is charged. None means neither the
    placement nor the metadata gives the dense size or the KV rate. Models with
    latent attention or sliding windows are over-counted (their KV is smaller),
    which fails safe.
    """
    size = max(0, int(file_size_bytes))
    expert_bytes: int | None = None
    if placement.dense_bytes is not None:
        dense = min(placement.dense_bytes, size) if size else placement.dense_bytes
        expert_bytes = max(0, size - dense) if size else None
    elif shape is not None and shape.expert_ff_length > 0 and size > 0:
        expert_params, dense_params = _expert_and_dense_params(shape)
        expert_bytes = size * expert_params // (expert_params + dense_params)
        dense = size - expert_bytes
    else:
        return None
    if placement.kv_bytes_per_token is not None:
        per_token = placement.kv_bytes_per_token
    elif shape is not None:
        per_token = kv_bytes_per_token(shape)
    else:
        return None
    window = int(num_ctx)
    if window <= 0:
        window = (placement.num_ctx
                  or (shape.context_length if shape is not None else 0)
                  or _DEFAULT_WINDOW)
    cache = placement.expert_cache_bytes
    if expert_bytes is not None:
        cache = min(cache, expert_bytes)
    total = (dense + per_token * window + placement.state_bytes
             + policy.streamed_workspace_bytes + cache + policy.host_overhead_bytes)
    return Footprint(total, total, FootprintSource.STREAMED, digest, window)


def select_footprint(
    *, measured_exact: Footprint | None, measured_other: Sequence[Footprint],
    file_size_bytes: int, requested_ctx: int, policy: GpuAdmissionPolicy, digest: str = "",
    streamed: Footprint | None = None,
) -> Footprint:
    """Measured beats estimated; a measurement at another window beats a guess.

    A ``streamed`` footprint (declared expert streaming) beats every measurement:
    a runner's working set counts file-backed expert pages that are not resident
    cost, so a sample of it would over-state the model.
    """
    if streamed is not None:
        return streamed
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
    "CtxDecision", "CtxPolicy", "ExpertModelShape", "Footprint", "FootprintSource",
    "GpuAdmissionPolicy", "HostFloorMode", "HostMemoryReading", "QueueEntry", "Queued",
    "RefusalReason", "Refused", "StreamedPlacement", "check_effective_ctx",
    "classify_operation", "estimate_host_footprint", "estimate_prompt_tokens",
    "estimate_streamed_footprint", "expert_model_shape", "host_memory_verdict",
    "is_zero_keep_alive", "kv_bytes_per_token", "priority_rank", "queue_order",
    "requested_num_ctx", "requested_reserve_tokens", "resolve_num_ctx", "select_footprint",
]
