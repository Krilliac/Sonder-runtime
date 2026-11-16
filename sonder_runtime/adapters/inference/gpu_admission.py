"""One admission gate for every Sonder call that reaches a local Ollama.

``GpuAdmission.admit`` is a context manager wrapped around the actual transport
call. Entering it decides, in order and from typed policy (``domain.gpu_admission``):

* the lane (PROBE / LOAD / INFERENCE / EMBED / UNLOAD);
* PROBE: a bounded in-process gate; no GPU slot, no memory check, never loads;
* otherwise, on a loopback origin: a place in the shared GPU queue (one slot
  per ``gpu_slots``, ``embed_slots`` for embeddings, interactive before
  background, FIFO within a class), then the ``num_ctx`` the model is served at
  (one window per loaded model; another window is served pinned or waits for
  unload, never a reload per request), then, for a cold load, whether host RAM
  and commit headroom cover the model's expected footprint.

Leaving it releases the slot and, off the request path, compares the window
sent with the one ``/api/ps`` reports and measures the runner's real footprint
after a cold load.

State and threads: the service owns in-process caches behind one lock; the
shared state (leases, pins, footprints) is in ``GpuAdmissionStore``, so other
processes see it. ``admit`` may be called from any thread. A ticket held in a
context makes a nested ``admit`` in that context pass through (no
self-deadlock); a different thread queues like any other caller.

Modes: ``enforce`` blocks and rewrites; ``observe`` makes the same decisions,
logs what it would have done and never blocks or rewrites; ``off`` is a
pass-through that touches nothing.
"""
from __future__ import annotations

import collections
import contextlib
import contextvars
import dataclasses
import importlib
import json
import logging
import os
import platform
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NoReturn, Protocol, cast

from sonder_runtime.adapters.inference.gpu_admission_store import (
    FULL, HELD, GpuAdmissionStore, StoreUnavailable,
)
from sonder_runtime.adapters.inference.runner_footprint import (
    MeasurementScheduler, measure_runner_for_blob, weights_blob_from_modelfile,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain import gpu_admission as policy
from sonder_runtime.domain import ollama_policy
from sonder_runtime.domain.gpu_admission import (
    AdmissionLane, CtxAction, CtxCheck, CtxPolicy, Footprint, FootprintSource,
    GpuAdmissionPolicy, HostFloorMode, HostMemoryReading, Queued, RefusalReason, Refused,
)
from sonder_runtime.domain.model_routing import is_cloud_model_name
from sonder_runtime.platform import host_memory
from sonder_runtime.platform.gpu_admission_config import (
    GpuAdmissionConfig, apply_gpu_admission_environment, gpu_admission_errors,
)

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_TAGS_CACHE_SECONDS = 30.0
_REMEASURE_SECONDS = 600.0
_READ_LIMIT = 4 * 1024 * 1024
_BOUND_LOG_TUPLES = 4096
_HELD_TICKET: contextvars.ContextVar[_Entry | None] = contextvars.ContextVar(
    "sonder_gpu_admission_held", default=None)

PsReader = Callable[[str], "Mapping[str, Any] | None"]


class _CtxMismatchMetrics(Protocol):
    """The one metrics call admission makes (``MetricsRegistry`` provides it)."""

    def observe_num_ctx_mismatch(self) -> None: ...


class GpuAdmissionRefused(ModelCallError):
    """A request was refused admission; the typed reason is ``refusal.reason``.

    ``transient`` is False and the pool never fails it over: another host is not
    a remedy for this host's memory floor or queue, and replaying it locally
    would only repeat the refusal. ``retry_after_seconds`` says when to ask again.
    """

    def __init__(self, refusal: Refused, *, attempts: int = 1, cloud: bool = False):
        self.refusal = refusal
        super().__init__(
            "admission",
            "GPU admission refused (%s): %s" % (refusal.reason.value, refusal.detail),
            transient=False, attempts=attempts, cloud=cloud,
            retry_after_seconds=refusal.retry_after_seconds,
        )

    def restamped(self, *, attempts: int, cloud: bool) -> "GpuAdmissionRefused":
        """The same refusal with the caller's attempt count (the caller's retry
        loop wraps every ``ModelCallError``; the type and evidence must survive)."""
        return GpuAdmissionRefused(self.refusal, attempts=attempts, cloud=cloud)


@dataclasses.dataclass
class AdmissionTicket:
    """What the caller sends: ``payload`` is the (possibly rewritten) request."""

    lane: AdmissionLane | None
    payload: Mapping[str, Any] | None
    requested_num_ctx: int | None = None
    served_num_ctx: int | None = None
    waited_seconds: float = 0.0
    rewritten: bool = False

    def body(self, original: bytes) -> bytes:
        """``original`` unless admission changed the payload, then its serialization."""
        return json.dumps(self.payload).encode("utf-8") if self.rewritten else original


def passthrough_ticket(payload: Mapping[str, Any] | None) -> AdmissionTicket:
    return AdmissionTicket(None, payload)


def _no_release() -> None:
    """The release of a probe that took no slot (off, observe, remote origin)."""


def _inconsistent_ctx_decision(decision: policy.CtxDecision) -> NoReturn:
    """Raise for a ``CtxDecision`` that ``policy.resolve_num_ctx`` never returns.

    ``resolve_num_ctx`` is the only producer of the decisions admission acts on,
    and it attaches a reason to every WAIT and REFUSE, a window to every
    SERVE_PINNED and INHERIT_PINNED, and returns PIN_NEW and REPIN only for a
    request that named a window. A decision that breaks this fails the request
    (in enforce mode ``_enter`` releases the slot), as the attribute or type
    error it would otherwise cause does.
    """
    raise RuntimeError("gpu admission: inconsistent num_ctx decision %r" % (decision,))


@dataclasses.dataclass
class _Entry:
    ticket: AdmissionTicket
    lane: AdmissionLane | None = None
    ticket_id: str = ""
    pool: str = ""
    origin: str = ""
    model_key: str = ""
    holds_slot: bool = False
    enqueued: bool = False
    cold: bool = False
    file_size: int = 0
    digest: str = ""
    store_ok: bool = True


@dataclasses.dataclass(frozen=True)
class _PsRow:
    name: str
    context_length: int | None
    size: int
    size_vram: int


@dataclasses.dataclass(frozen=True)
class _TagRow:
    digest: str
    size: int


@dataclasses.dataclass
class _Plan:
    ctx: policy.CtxDecision
    requested: int | None
    cold: bool
    digest: str
    file_size: int
    follow_effective: int | None
    memory: object | None
    footprint: Footprint | None
    reading: HostMemoryReading | None
    evictable: tuple[str, ...]


def model_key(name: object) -> str:
    text = str(name or "").strip().casefold()
    return text[:-7] if text.endswith(":latest") else text


def default_store_path(env: Mapping[str, str] | None = None) -> Path:
    """The machine-wide store: shared by every Sonder instance of this user.

    Deliberately not under ``SONDER_HOME``: the GPU is per machine, and a
    worktree instance with its own state home must queue behind production.
    """
    values = os.environ if env is None else env
    if sys.platform == "win32" or os.name == "nt":
        root = str(values.get("LOCALAPPDATA") or values.get("APPDATA") or "").strip()
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return base / "Sonder" / "gpu-admission.sqlite3"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "sonder" / "gpu-admission.sqlite3"
    state = str(values.get("XDG_STATE_HOME") or "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "sonder" / "gpu-admission.sqlite3"


def policy_from_config(config: GpuAdmissionConfig) -> GpuAdmissionPolicy:
    return GpuAdmissionPolicy(
        min_available_ram_bytes=config.min_available_ram_mb * _MB,
        min_commit_headroom_bytes=config.min_commit_headroom_mb * _MB,
        on_host_floor=HostFloorMode(config.on_host_floor),
        ctx_conflict=CtxPolicy(config.ctx_conflict),
        max_repins_per_model=config.max_repins_per_model,
        repin_window_seconds=config.repin_window_seconds,
        unmeasured_host_factor_pct=config.unmeasured_host_factor_pct,
        host_overhead_bytes=config.host_overhead_mb * _MB,
    )


def _read_json(origin: str, path: str, timeout: float = 2.0) -> Mapping[str, Any] | None:
    """GET one control-plane document through the policy-enforcing transport."""
    ollama_endpoint = importlib.import_module("sonder_runtime.adapters.inference.ollama_endpoint")
    try:
        request = urllib.request.Request(origin + path, method="GET")
        with ollama_endpoint.open_url(request, timeout=timeout) as response:
            raw = response.read(_READ_LIMIT + 1)
        if len(raw) > _READ_LIMIT:
            return None
        value = json.loads(raw.decode("utf-8"))
    except Exception:  # a failed read means "unknown", never a failed request
        logger.debug("gpu admission could not read %s%s", origin, path, exc_info=True)
        return None
    return value if isinstance(value, Mapping) else None


def _default_host_reader() -> HostMemoryReading | None:
    reading = host_memory.read_host_memory()
    if reading is None:
        return None
    return HostMemoryReading(
        available_physical_bytes=reading.available_physical_bytes,
        total_physical_bytes=reading.total_physical_bytes,
        commit_limit_bytes=reading.commit_limit_bytes,
        commit_available_bytes=reading.commit_available_bytes,
        commit_enforced=reading.commit_enforced,
    )


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class GpuAdmission:
    """The admission service; one per process (see ``process_service``)."""

    def __init__(
        self,
        config: GpuAdmissionConfig | None = None,
        *,
        store: GpuAdmissionStore | None = None,
        host_reader: Callable[[], HostMemoryReading | None] | None = None,
        ps_reader: PsReader | None = None,
        tags_reader: PsReader | None = None,
        show_reader: Callable[[str, str], Mapping[str, Any] | None] | None = None,
        memory_reader: Callable[[str], tuple[int, int] | None] | None = None,
        metrics: _CtxMismatchMetrics | None = None,
        background: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or GpuAdmissionConfig()
        problems = gpu_admission_errors(self.config)
        if problems:
            raise ValueError("; ".join(problems))
        self._policy = policy_from_config(self.config)
        self._store = store
        self._store_error = ""
        self._verified = False
        self._host_reader = host_reader or _default_host_reader
        self._ps_reader = ps_reader or (lambda origin: _read_json(origin, "/api/ps"))
        self._tags_reader = tags_reader or (lambda origin: _read_json(origin, "/api/tags"))
        self._show_reader = show_reader or (
            lambda origin, model: self._show_modelfile(origin, model))
        self._memory_reader = memory_reader
        self._metrics = metrics
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._wake = threading.Condition()
        self._probe_sem = threading.BoundedSemaphore(self.config.probe_slots)
        self._measurements = MeasurementScheduler(background=background)
        self._background = background or self._spawn
        self._ps_cache: dict[str, tuple[float, dict[str, _PsRow]]] = {}
        self._tags_cache: dict[str, tuple[float, dict[str, _TagRow]]] = {}
        self._pin_warned: set[tuple[str, str, int | None, int]] = set()
        self._ctx_checked: dict[tuple[str, str], float] = {}
        self._mismatch_seen: set[tuple[str, str, int, int | None]] = set()
        self._unverifiable_seen: set[tuple[str, str]] = set()
        self._last_measured: dict[tuple[str, int], float] = {}
        self._store_errors_logged: set[str] = set()
        self._counters: collections.Counter[str] = collections.Counter()
        self._last_refusals: collections.deque[tuple[str, str]] = collections.deque(maxlen=8)
        self._last_reading: HostMemoryReading | None = None
        self._pins_seen: dict[str, int] = {}

    # -- plumbing -------------------------------------------------------------
    @staticmethod
    def _spawn(work: Callable[[], None]) -> None:
        from sonder_runtime.platform.runtime_threads import Thread
        # ``Thread`` is unannotated in its module; both of its paths return a thread.
        cast("Callable[..., threading.Thread]", Thread)(
            target=work, daemon=True, name="sonder-gpu-admission").start()

    @property
    def mode(self) -> str:
        return self.config.mode

    def _the_store(self) -> GpuAdmissionStore:
        """The shared store, verified once; a failed operation re-verifies next time."""
        if self._store is None:
            configured = self.config.store_path
            self._store = GpuAdmissionStore(configured or default_store_path())
        if not self._verified:
            try:
                self._store.verify()
            except StoreUnavailable as error:
                self._store_error = str(error)
                raise
            self._verified = True
            self._store_error = ""
        return self._store

    def _store_failed(self, error: StoreUnavailable) -> None:
        self._verified = False
        self._store_error = str(error)

    def _notify(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._wake:
            self._wake.wait(seconds)

    def record_footprint(self, digest: str, num_ctx: int, working_set_bytes: int,
                         private_bytes: int) -> None:
        self._the_store().record_footprint(digest, num_ctx, working_set_bytes, private_bytes)

    # -- control-plane snapshots ------------------------------------------------
    def _read_ps(self, origin: str, *, force: bool = False) -> dict[str, _PsRow] | None:
        now = self._clock()
        cached = self._ps_cache.get(origin)
        if cached is not None and not force and now - cached[0] < self.config.ps_cache_ms / 1000.0:
            return cached[1]
        payload = self._ps_reader(origin)
        models = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(models, list):
            self._ps_cache.pop(origin, None)
            return None
        rows: dict[str, _PsRow] = {}
        for item in models:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("model") or "")
            if name:
                rows[model_key(name)] = _PsRow(
                    name, _positive(item.get("context_length")),
                    int(item.get("size") or 0), int(item.get("size_vram") or 0))
        self._ps_cache[origin] = (now, rows)
        return rows

    def _read_tags(self, origin: str) -> dict[str, _TagRow]:
        now = self._clock()
        cached = self._tags_cache.get(origin)
        if cached is not None and now - cached[0] < _TAGS_CACHE_SECONDS:
            return cached[1]
        payload = self._tags_reader(origin)
        rows: dict[str, _TagRow] = {}
        models = payload.get("models") if isinstance(payload, Mapping) else None
        for item in models if isinstance(models, list) else ():
            if not isinstance(item, Mapping):
                continue
            digest = str(item.get("digest") or "").lower().removeprefix("sha256:")
            for field in ("name", "model"):
                if item.get(field):
                    rows[model_key(item[field])] = _TagRow(digest, int(item.get("size") or 0))
        if rows:
            self._tags_cache[origin] = (now, rows)
        return rows

    def _show_modelfile(self, origin: str, model: str) -> Mapping[str, Any] | None:
        ollama_endpoint = importlib.import_module("sonder_runtime.adapters.inference.ollama_endpoint")
        try:
            request = urllib.request.Request(
                origin + "/api/show", data=json.dumps({"model": model, "name": model}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with ollama_endpoint.open_url(request, timeout=5.0) as response:
                value = json.loads(response.read(_READ_LIMIT).decode("utf-8"))
        except Exception:
            logger.debug("gpu admission could not read /api/show for %s", model, exc_info=True)
            return None
        return value if isinstance(value, Mapping) else None

    # -- public API -------------------------------------------------------------
    @contextlib.contextmanager
    def admit(
        self, path: str, payload: Mapping[str, Any] | None, *, origin: str,
        deadline: float | None = None, caller: str = "interactive",
        prewarm: bool = False, ctx_policy: CtxPolicy | None = None,
    ) -> Iterator[AdmissionTicket]:
        """Gate one request. ``deadline`` is an absolute ``time.monotonic()`` value."""
        mode = self.config.mode
        if mode == "off":
            yield passthrough_ticket(payload)
            return
        if _HELD_TICKET.get() is not None:
            logger.debug("gpu admission: nested %s passes through the held ticket", path)
            yield passthrough_ticket(payload)
            return
        body = payload if isinstance(payload, Mapping) else None
        lane = policy.classify_operation(
            path, body, method="GET" if payload is None else "POST", prewarm=prewarm)
        if lane is AdmissionLane.MANAGE:
            yield passthrough_ticket(payload)
            return
        if lane is AdmissionLane.PROBE:
            release = self.acquire_probe(path, origin=origin, deadline=deadline)
            try:
                yield passthrough_ticket(payload)
            finally:
                release()
            return
        key = model_key(body.get("model") if body is not None else None)
        # ``body is None`` already gives ``key == ""``; naming it lets the gated
        # path below take a request body that is known to be present.
        if body is None or not key or is_cloud_model_name(key):
            yield passthrough_ticket(payload)
            return
        if lane is AdmissionLane.UNLOAD:
            yield passthrough_ticket(payload)
            self._after_unload(origin, key)
            return
        entry = self._enter(lane, body, origin, key, deadline, caller, ctx_policy)
        token = _HELD_TICKET.set(entry)
        succeeded = False
        try:
            yield entry.ticket
            succeeded = True
        finally:
            _HELD_TICKET.reset(token)
            self._leave(entry, succeeded)

    # -- PROBE lane ---------------------------------------------------------------
    def acquire_probe(self, path: str, *, origin: str,
                      deadline: float | None = None) -> Callable[[], None]:
        """Take one PROBE slot for a control-plane read of ``origin``; return its release.

        Only loopback origins are gated: the slots keep probes from piling onto
        the local Ollama (and so the local GPU); a remote worker's probe never
        waits behind them. The wait is bounded by ``probe_wait_ms`` and, when
        given, ``deadline``; running out raises ``GpuAdmissionRefused(PROBE_BUSY)``
        (observe mode logs it and admits without a slot). The returned release
        is thread-agnostic and idempotent: only its first call frees the slot,
        so a caller may hand it to whichever thread finishes the read.
        """
        if self.config.mode == "off" or not ollama_policy.is_loopback(origin):
            return _no_release
        wait = self.config.probe_wait_ms / 1000.0
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - self._clock()))
        if not self._probe_sem.acquire(timeout=wait):
            refusal = Refused(
                RefusalReason.PROBE_BUSY,
                "%d probe(s) already in flight for %s; none freed within %d ms"
                % (self.config.probe_slots, path, int(wait * 1000)),
                retry_after_seconds=max(0.1, wait))
            if self.config.mode == "observe":
                logger.warning("gpu admission (observe): would refuse probe: %s", refusal.detail)
                return _no_release
            self._refuse(refusal)
        once = threading.Lock()

        def release() -> None:
            if once.acquire(blocking=False):
                self._probe_sem.release()

        return release

    # -- LOAD / INFERENCE / EMBED lanes -------------------------------------------
    def _refuse(self, refusal: Refused) -> NoReturn:
        with self._lock:
            self._counters["refused"] += 1
            self._counters["refused_" + refusal.reason.value] += 1
            self._last_refusals.append((refusal.reason.value, refusal.detail[:200]))
        logger.warning("gpu admission refused (%s): %s", refusal.reason.value, refusal.detail)
        raise GpuAdmissionRefused(refusal)

    def _store_unavailable(self, error: StoreUnavailable) -> None:
        self._store_failed(error)
        self._refuse(Refused(RefusalReason.STORE_UNAVAILABLE, str(error), retry_after_seconds=5.0))

    def _enter(
        self, lane: AdmissionLane, body: Mapping[str, Any], origin: str, key: str,
        deadline: float | None, caller: str, ctx_policy: CtxPolicy | None,
    ) -> _Entry:
        ticket = AdmissionTicket(lane, body, requested_num_ctx=policy.requested_num_ctx(body))
        entry = _Entry(ticket, lane=lane, origin=origin, model_key=key,
                       ticket_id=uuid.uuid4().hex,
                       pool="embed" if lane is AdmissionLane.EMBED else "gpu")
        ticket.served_num_ctx = ticket.requested_num_ctx
        if self.config.mode == "observe":
            self._observe(entry, body, deadline, ctx_policy)
            return entry
        try:
            self._enforce(entry, body, deadline, caller, ctx_policy)
        except StoreUnavailable as error:
            self._release_quietly(entry)
            self._store_unavailable(error)
        except BaseException:
            self._release_quietly(entry)
            raise
        return entry

    def _ttl(self, deadline: float | None) -> float:
        remaining = (deadline - self._clock()) if deadline is not None else 600.0
        return max(1.0, remaining) + self.config.lease_ttl_margin_seconds

    def _take_slot(self, entry: _Entry, limit: float, deadline: float | None, caller: str) -> None:
        store = self._the_store()
        interval = self.config.poll_interval_ms / 1000.0
        slots = self.config.embed_slots if entry.pool == "embed" else self.config.gpu_slots
        waited = False
        entry.enqueued = True
        while True:
            state = store.acquire(
                entry.ticket_id, entry.pool, slots=slots, priority=policy.priority_rank(caller),
                origin=entry.origin, model_key=entry.model_key, ttl_seconds=self._ttl(deadline),
                max_depth=self.config.max_queue_depth)
            if state == HELD:
                entry.holds_slot = True
                return
            if state == FULL:
                self._refuse(Refused(
                    RefusalReason.QUEUE_FULL,
                    "%d requests already wait for the %s GPU queue" % (self.config.max_queue_depth, entry.pool),
                    retry_after_seconds=5.0))
            if not waited:
                waited = True
                with self._lock:
                    self._counters["queued"] += 1
            if self._clock() >= limit:
                store.release(entry.ticket_id)
                self._refuse(Refused(
                    RefusalReason.QUEUE_TIMEOUT,
                    "waited %.0f s for a free %s GPU slot" % (
                        self.config.max_queue_wait_seconds, entry.pool),
                    retry_after_seconds=5.0))
            self._pause(min(interval, max(0.0, limit - self._clock())))

    def _release_quietly(self, entry: _Entry) -> None:
        if entry.enqueued and self._store is not None:
            try:
                self._store.release(entry.ticket_id)
            except StoreUnavailable:
                logger.warning("gpu admission could not release ticket %s", entry.ticket_id)
            entry.holds_slot = False
            entry.enqueued = False
            self._notify()

    def _enforce(
        self, entry: _Entry, body: Mapping[str, Any], deadline: float | None, caller: str,
        ctx_policy: CtxPolicy | None,
    ) -> None:
        started = self._clock()
        limit = started + self.config.max_queue_wait_seconds
        if deadline is not None:
            limit = min(limit, deadline)
        local = ollama_policy.is_loopback(entry.origin)
        store = self._the_store()
        while True:
            if local:
                self._take_slot(entry, limit, deadline, caller)
            plan = self._plan(entry, body, local, ctx_policy)
            wait_interval = None
            refusal = None
            if plan.ctx.action is CtxAction.REFUSE:
                if plan.ctx.reason is None:  # resolve_num_ctx gives every REFUSE a reason
                    _inconsistent_ctx_decision(plan.ctx)
                refusal = Refused(plan.ctx.reason, plan.ctx.detail, retry_after_seconds=30.0)
            elif plan.ctx.action is CtxAction.WAIT:
                wait_interval = self.config.poll_interval_ms / 1000.0
                refusal = Refused(
                    RefusalReason.CTX_PIN_CONFLICT,
                    "%s: model %s is loaded at its pinned window until it unloads"
                    % (plan.ctx.detail, entry.model_key), retry_after_seconds=30.0)
            elif isinstance(plan.memory, Queued):
                wait_interval = self.config.host_recheck_interval_ms / 1000.0
                refusal = Refused(
                    plan.memory.reason, plan.memory.detail, plan.memory.retry_after_seconds,
                    plan.reading, plan.footprint, plan.evictable)
            elif isinstance(plan.memory, Refused):
                refusal = plan.memory
            if refusal is None:
                break
            if wait_interval is None or self._clock() + wait_interval >= limit:
                self._release_quietly(entry)
                self._refuse(refusal)
            self._release_quietly(entry)
            with self._lock:
                self._counters["queued"] += 1
            self._pause(wait_interval)
        self._apply(entry, plan, body, store)
        entry.ticket.waited_seconds = self._clock() - started
        with self._lock:
            self._counters["admitted"] += 1

    def _observe(
        self, entry: _Entry, body: Mapping[str, Any], deadline: float | None,
        ctx_policy: CtxPolicy | None,
    ) -> None:
        local = ollama_policy.is_loopback(entry.origin)
        try:
            self._the_store()
            plan = self._plan(entry, body, local, ctx_policy)
        except StoreUnavailable as error:
            entry.store_ok = False
            self._store_failed(error)
            text = str(error)
            if text not in self._store_errors_logged:
                self._store_errors_logged.add(text)
                logger.warning("gpu admission (observe): %s; admitting without coordination", text)
            return
        entry.cold, entry.digest, entry.file_size = plan.cold, plan.digest, plan.file_size
        if plan.ctx.action in (CtxAction.REFUSE, CtxAction.WAIT):
            if plan.ctx.reason is None:  # resolve_num_ctx gives every WAIT and REFUSE a reason
                _inconsistent_ctx_decision(plan.ctx)
            logger.warning("gpu admission (observe): would refuse %s: %s",
                           plan.ctx.reason.value, plan.ctx.detail)
        elif plan.ctx.action in (CtxAction.SERVE_PINNED, CtxAction.INHERIT_PINNED):
            logger.warning("gpu admission (observe): would serve %s at pinned num_ctx=%s (%s)",
                           entry.model_key, plan.ctx.num_ctx, plan.ctx.detail or "no window requested")
        if isinstance(plan.memory, (Queued, Refused)):
            logger.warning("gpu admission (observe): would not admit cold load of %s: %s",
                           entry.model_key, plan.memory.detail)
        with self._lock:
            self._counters["observed"] += 1

    # -- decisions ---------------------------------------------------------------
    def _plan(
        self, entry: _Entry, body: Mapping[str, Any], local: bool, ctx_policy: CtxPolicy | None,
    ) -> _Plan:
        store = self._the_store()
        origin, key, lane = entry.origin, entry.model_key, entry.lane
        requested = policy.requested_num_ctx(body)
        rows = self._read_ps(origin)
        row = rows.get(key) if rows is not None else None
        pin = store.get_pin(origin, key)
        holders = store.holders(origin, key, exclude=entry.ticket_id) if local else 0
        effective = row.context_length if row is not None else None
        follow = None
        if row is not None:
            current = effective if effective is not None else (pin.num_ctx if pin else None)
            if effective is not None and (pin is None or pin.num_ctx != effective):
                follow = effective
        else:
            current = pin.num_ctx if (pin is not None and holders > 0) else None
        decision = policy.resolve_num_ctx(
            requested=requested, current=current,
            policy=ctx_policy or self._policy.ctx_conflict, holders=holders,
            prompt_tokens=policy.estimate_prompt_tokens(body, self._policy.chars_per_token),
            reserve_tokens=policy.requested_reserve_tokens(body),
            repins_in_window=store.repins_in_window(origin, key, self._policy.repin_window_seconds),
            max_repins=self._policy.max_repins_per_model)
        # An unreadable /api/ps says nothing about residency: "unknown" is not
        # "cold", so it never triggers a host-memory refusal or a measurement.
        cold = rows is not None and row is None and holders == 0
        tag = self._read_tags(origin).get(key)
        digest = tag.digest if tag and tag.digest else "name:" + key
        file_size = tag.size if tag else 0
        memory = footprint = reading = None
        evictable: tuple[str, ...] = ()
        if (local and cold and decision.action not in (CtxAction.WAIT, CtxAction.REFUSE)
                and lane in (AdmissionLane.LOAD, AdmissionLane.INFERENCE, AdmissionLane.EMBED)):
            reading = self._host_reader()
            if reading is not None:
                self._last_reading = reading
                served = decision.num_ctx if decision.num_ctx is not None else (requested or 0)
                records = [
                    Footprint(r.working_set_bytes, r.private_bytes, FootprintSource.MEASURED,
                              r.digest, r.num_ctx)
                    for r in store.footprints_for(digest)]
                footprint = policy.select_footprint(
                    measured_exact=next((r for r in records if r.num_ctx == served), None),
                    measured_other=[r for r in records if r.num_ctx != served],
                    file_size_bytes=file_size, requested_ctx=served, policy=self._policy,
                    digest=digest)
                evictable = self._evictable(rows, key)
                memory = policy.host_memory_verdict(
                    reading, footprint, policy=self._policy, lane=lane, evictable=evictable,
                    retry_after_seconds=self.config.host_recheck_interval_ms / 1000.0)
            else:
                logger.debug("gpu admission: host memory unreadable; cold load of %s not checked", key)
        return _Plan(decision, requested, cold, digest, file_size, follow, memory,
                     footprint, reading, evictable)

    @staticmethod
    def _evictable(rows: Mapping[str, _PsRow] | None, key: str) -> tuple[str, ...]:
        if not rows:
            return ()
        gib = 1024.0 ** 3
        return tuple("%s (%.1f GiB loaded)" % (row.name, row.size / gib)
                     for other, row in sorted(rows.items()) if other != key)[:8]

    def _apply(
        self, entry: _Entry, plan: _Plan, body: Mapping[str, Any], store: GpuAdmissionStore,
    ) -> None:
        ticket, origin, key = entry.ticket, entry.origin, entry.model_key
        entry.cold, entry.digest, entry.file_size = plan.cold, plan.digest, plan.file_size
        if plan.follow_effective is not None:
            store.set_pin(origin, key, plan.follow_effective)
        action, requested, served = plan.ctx.action, plan.requested, plan.ctx.num_ctx
        if action is CtxAction.PIN_NEW:
            if requested is None:  # resolve_num_ctx pins only a window the request named
                _inconsistent_ctx_decision(plan.ctx)
            store.set_pin(origin, key, requested)
        elif action is CtxAction.REPIN:
            if requested is None:  # resolve_num_ctx repins only to a window the request named
                _inconsistent_ctx_decision(plan.ctx)
            store.set_pin(origin, key, requested, repin=True,
                          window_seconds=self._policy.repin_window_seconds)
            logger.warning(
                "num_ctx repin: model=%s origin=%s requested=%d replaces the pinned window; "
                "the model reloads once (%s)", key, origin, requested, plan.ctx.detail)
        elif action in (CtxAction.SERVE_PINNED, CtxAction.INHERIT_PINNED):
            if served is None:  # resolve_num_ctx serves a pin only with its window
                _inconsistent_ctx_decision(plan.ctx)
            ticket.payload = self._with_num_ctx(body, served)
            ticket.rewritten = True
            ticket.served_num_ctx = served
            if action is CtxAction.SERVE_PINNED:
                tuple_key = (origin, key, requested, served)
                with self._lock:
                    first = (tuple_key not in self._pin_warned
                             and len(self._pin_warned) < _BOUND_LOG_TUPLES)
                    if first:
                        self._pin_warned.add(tuple_key)
                    self._counters["served_pinned"] += 1
                if first:
                    logger.warning(
                        "num_ctx pinned: model=%s requested=%d is served at the loaded window "
                        "pinned=%d to avoid a reload (origin=%s)", key, requested, served, origin)
            else:
                logger.debug("num_ctx inherited: model=%s request named no window; using pinned=%d",
                             key, served)
        else:
            store.touch_pin(origin, key)
        if action in (CtxAction.PIN_NEW, CtxAction.REPIN, CtxAction.SERVE_AS_REQUESTED):
            ticket.served_num_ctx = requested

    @staticmethod
    def _with_num_ctx(body: Mapping[str, Any], num_ctx: int) -> dict[str, Any]:
        rewritten = dict(body)
        options = dict(rewritten.get("options") or {})
        options["num_ctx"] = num_ctx
        rewritten["options"] = options
        return rewritten

    # -- leaving -------------------------------------------------------------------
    def _leave(self, entry: _Entry, succeeded: bool) -> None:
        self._release_quietly(entry)
        self._ps_cache.pop(entry.origin, None)
        if succeeded and entry.ticket.lane in (
                AdmissionLane.LOAD, AdmissionLane.INFERENCE, AdmissionLane.EMBED):
            try:
                self._background(lambda: self._after_response(entry))
            except Exception:
                logger.debug("gpu admission could not schedule post-response checks", exc_info=True)

    def _after_unload(self, origin: str, key: str) -> None:
        try:
            self._the_store().clear_pin(origin, key)
        except StoreUnavailable as error:
            logger.debug("gpu admission could not clear the pin of %s: %s", key, error)
        self._ps_cache.pop(origin, None)

    def _after_response(self, entry: _Entry) -> None:
        try:
            self._check_effective_ctx(entry)
        except Exception:
            logger.debug("num_ctx check failed", exc_info=True)
        try:
            self._maybe_measure(entry)
        except Exception:
            logger.debug("footprint scheduling failed", exc_info=True)

    def _check_effective_ctx(self, entry: _Entry) -> None:
        sent = entry.ticket.served_num_ctx
        if sent is None:
            return
        slot = (entry.origin, entry.model_key)
        now = self._clock()
        with self._lock:
            last = self._ctx_checked.get(slot)
            due = last is None or now - last >= self.config.ctx_check_interval_seconds
        if not due:
            return
        rows = self._read_ps(entry.origin, force=True)
        row = rows.get(entry.model_key) if rows is not None else None
        if row is None:
            return
        with self._lock:
            self._ctx_checked[slot] = now
        verdict = policy.check_effective_ctx(sent, row.context_length)
        if verdict is CtxCheck.MISMATCH:
            tuple_key = (entry.origin, entry.model_key, sent, row.context_length)
            with self._lock:
                first = (tuple_key not in self._mismatch_seen
                         and len(self._mismatch_seen) < _BOUND_LOG_TUPLES)
                if first:
                    self._mismatch_seen.add(tuple_key)
                    self._counters["num_ctx_mismatches"] += 1
            if first:
                logger.error(
                    "num_ctx mismatch: model=%s origin=%s sent=%d effective=%d; the server runs "
                    "the model at a different window than was requested",
                    entry.model_key, entry.origin, sent, row.context_length)
                if self._metrics is not None:
                    self._metrics.observe_num_ctx_mismatch()
        elif verdict is CtxCheck.UNVERIFIABLE:
            with self._lock:
                first = slot not in self._unverifiable_seen
                self._unverifiable_seen.add(slot)
                if first:
                    self._counters["num_ctx_unverifiable"] += 1
            if first:
                logger.info(
                    "num_ctx unverifiable: model=%s origin=%s; /api/ps reports no context_length, "
                    "so the sent window %s cannot be compared", entry.model_key, entry.origin, sent)

    def _maybe_measure(self, entry: _Entry) -> None:
        if not ollama_policy.is_loopback(entry.origin) or self._store is None:
            return
        served = entry.ticket.served_num_ctx
        if served is None:
            return
        slot = (entry.digest, served)
        now = self._clock()
        with self._lock:
            last = self._last_measured.get(slot)
            due = (last is None and entry.cold) or (
                last is not None and now - last >= _REMEASURE_SECONDS)
            if not due or self._measurements.busy:
                return
            self._last_measured[slot] = now
        self._measurements.schedule(lambda: self._measure(entry.origin, entry.model_key,
                                                          entry.digest, served))

    def _measure(self, origin: str, key: str, digest: str, num_ctx: int) -> None:
        reader = self._memory_reader
        if reader is not None:
            measured = reader(key)
        else:
            modelfile = self._show_reader(origin, key)
            blob = weights_blob_from_modelfile(str(modelfile.get("modelfile") or "")) if modelfile else None
            if blob is None:
                logger.debug("gpu admission: no weights blob for %s; footprint not measured", key)
                return
            found = measure_runner_for_blob(blob, self.config.runner_process_names)
            measured = (found.working_set_bytes, found.private_bytes) if found else None
        if measured is None:
            logger.debug("gpu admission: no runner found for %s; footprint not measured", key)
            return
        self._the_store().record_footprint(digest, num_ctx, measured[0], measured[1])
        gib = 1024.0 ** 3
        logger.info("host footprint measured: model=%s num_ctx=%d working_set=%.1f GiB private=%.1f GiB",
                    key, num_ctx, measured[0] / gib, measured[1] / gib)

    # -- status ----------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Bounded, I/O-free state for ``pool.summary()`` and ``status()``."""
        with self._lock:
            counters = dict(self._counters)
            refusals = [{"reason": reason, "detail": detail} for reason, detail in self._last_refusals]
            reading = self._last_reading
            loaded = sorted(
                {row.name: row.context_length for _t, rows in list(self._ps_cache.values())
                 for row in rows.values()}.items(), key=str)[:16]
        return {
            "mode": self.config.mode,
            "store": {"path": str(getattr(self._store, "path", "") or ""), "error": self._store_error},
            "gpu_slots": self.config.gpu_slots,
            "embed_slots": self.config.embed_slots,
            "probe_slots": self.config.probe_slots,
            "admitted": counters.get("admitted", 0),
            "queued": counters.get("queued", 0),
            "refused": counters.get("refused", 0),
            "served_pinned": counters.get("served_pinned", 0),
            "num_ctx_mismatches": counters.get("num_ctx_mismatches", 0),
            "num_ctx_unverifiable": counters.get("num_ctx_unverifiable", 0),
            "loaded": [{"model": name, "num_ctx": ctx} for name, ctx in loaded],
            "host": None if reading is None else {
                "available_physical_bytes": reading.available_physical_bytes,
                "commit_available_bytes": reading.commit_available_bytes,
                "commit_enforced": reading.commit_enforced,
            },
            "last_refusals": refusals,
        }


# -- process-wide service and typed binding ---------------------------------------------
_configuration_lock = threading.RLock()
_typed_config: GpuAdmissionConfig | None = None
_process_service: GpuAdmission | None = None


def config_from_environment(environment: Mapping[str, str] | None = None) -> GpuAdmissionConfig:
    """The configuration an environment selects; raises ``ValueError`` if invalid."""
    errors: list[str] = []
    env = os.environ if environment is None else environment
    config = apply_gpu_admission_environment(GpuAdmissionConfig(), env, errors)
    errors.extend(gpu_admission_errors(config))
    if errors:
        raise ValueError("invalid GPU admission configuration: " + "; ".join(errors))
    return config


def service_for_environment(environment: Mapping[str, str]) -> GpuAdmission:
    """A service for an explicit environment mapping (tests, process-local callers).

    Such a mapping does not opt into the machine-wide store by saying nothing:
    admission is ``off`` unless it names ``SONDER_GPU_ADMISSION`` itself."""
    config = config_from_environment(environment)
    if "SONDER_GPU_ADMISSION" not in environment:
        config = dataclasses.replace(config, mode="off")
    return GpuAdmission(config)


def configure_typed_gpu_admission(config: GpuAdmissionConfig) -> None:
    """Bind the validated ``[gpu_admission]`` section for this process.

    Called next to ``configure_typed_workers``, before the pool is built. The
    documented ``SONDER_GPU_*`` environment overrides apply to the section here
    too, so a section built in code obeys them like one loaded from TOML."""
    global _typed_config, _process_service
    errors: list[str] = []
    config = apply_gpu_admission_environment(config, os.environ, errors)
    problems = errors + gpu_admission_errors(config)
    if problems:
        raise ValueError("; ".join(problems))
    with _configuration_lock:
        _typed_config = config
        _process_service = None


def reset_typed_gpu_admission() -> None:
    global _typed_config, _process_service
    with _configuration_lock:
        _typed_config = None
        _process_service = None


def typed_config() -> GpuAdmissionConfig | None:
    with _configuration_lock:
        return _typed_config


def process_service() -> GpuAdmission:
    """The one service this process shares (embeddings, vision, the pool)."""
    global _process_service
    with _configuration_lock:
        if _process_service is None:
            config = _typed_config if _typed_config is not None else config_from_environment()
            from sonder_runtime.platform.metrics import default_registry
            _process_service = GpuAdmission(config, metrics=default_registry())
        return _process_service


def set_process_service(service: GpuAdmission | None) -> None:
    """Replace the process service (tests and embedding hosts)."""
    global _process_service
    with _configuration_lock:
        _process_service = service


__all__ = [
    "AdmissionTicket", "GpuAdmission", "GpuAdmissionRefused", "StoreUnavailable",
    "config_from_environment", "configure_typed_gpu_admission", "default_store_path",
    "model_key", "passthrough_ticket", "policy_from_config", "process_service",
    "reset_typed_gpu_admission", "service_for_environment", "set_process_service", "typed_config",
]
