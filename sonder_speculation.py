"""Speculative execution and branch prediction for the orchestration loop.

The agent loop is serial in the same way a naive CPU pipeline is: the
model "decides" the next tool, then the tool runs, then the model decides
again.  Two microarchitectural ideas map directly onto Sonder's latency:

- **Branch prediction.**  A predictor learns, from grounded history,
  which tool (and arguments shape) tends to follow a given loop state and
  which model tier a given kind of request opens with.  Predictions are
  hints, never authority.

- **Speculative execution.**  While the model is still generating its
  next decision, the host may *speculatively* run the predicted tool call
  **only when it is read-only** — exactly as a CPU speculatively executes
  but never commits stores past an unresolved branch.  If the model's
  real decision matches the prediction, the speculative observation is
  retired (a cache hit, latency hidden); otherwise it is squashed and
  discarded.  Mutating tools are never speculated.

Everything here is advisory and side-effect-free on the domain: a
mispredict costs at most one wasted read-only call, and the predictor
persists as a small JSON file so it warms across runs.  Correctness never
depends on a prediction being right.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import sonder_paths

# Read-only tools that are safe to run speculatively.  This is intentionally
# a strict allowlist: a tool is speculatable only if running it before the
# model commits can never change durable state, spend a cloud budget, or
# reach the network.  Mutations, execution, web, and model-spawning tools
# are excluded by omission.
SPECULATABLE_TOOLS = frozenset({
    "workspace_inventory",
    "directory_tree",
    "file_find",
    "file_read",
    "file_read_range",
    "text_search",
    "script_search",
    "program_search",
    "image_inspect",
    "data_inspect",
    "memory_search",
    "activity_status",
    "context_health",
    "status",
    "command_registry_list",
    "permission_policy",
})

_PREDICTOR_VERSION = 1
_MAX_TRANSITIONS = 4000
_MIN_CONFIDENCE = 0.34  # below a two-way coin flip's edge -> do not speculate

# Adaptive cost model.  Speculation only pays when the wall time it can hide
# is worth the wasted read-only call a mispredict costs.  The hidden overlap
# per correct speculation is the *shorter* of the model-decision latency and
# the speculated tool's latency (you can hide at most the whole tool inside
# the decision, or the whole decision behind the tool).  On a CPU serving a
# small model the tools are milliseconds while decisions are seconds, so the
# hideable overlap is ~0 and speculation stays dormant; on serious hardware
# serving a large model with genuinely slow tools (big scans, remote reads)
# both are substantial and it engages.  The predictor learns both latencies
# on the machine it actually runs on and persists them, so the decision is
# never a guess about the deployment.
_SPEC_EWMA_ALPHA = 0.25            # weight on the newest latency sample
_SPEC_WARMUP = 16                  # speculations allowed to measure before gating
_DEFAULT_MIN_SAVING_MS = 40.0      # expected hidden wall time below this -> skip

# Reorder buffer sizing.  A CPU commits speculative loads out of a small
# buffer; the analog here is a handful of read-only tool calls in flight at
# once.  The default is a single slot so behavior is byte-for-byte identical
# to the original single-in-flight engine unless the operator opts in, and it
# is clamped to a small maximum because each slot is a real worker thread and
# a wasted read-only call on a mispredict.
_DEFAULT_SLOTS = 1
_MAX_SLOTS = 4


def min_saving_seconds() -> float:
    """Floor on expected hidden wall time for a speculation to be worth it."""
    raw = os.environ.get("SONDER_SPECULATION_MIN_SAVING_MS", "").strip()
    try:
        ms = float(raw) if raw else _DEFAULT_MIN_SAVING_MS
    except ValueError:
        ms = _DEFAULT_MIN_SAVING_MS
    return max(0.0, ms) / 1000.0


def speculation_slots() -> int:
    """How many read-only speculations may be in flight at once.

    Read from ``SONDER_SPECULATION_SLOTS`` at call time (never at import).
    Defaults to a single slot so the engine preserves its original
    single-in-flight semantics, and is clamped to ``[1, _MAX_SLOTS]`` so a
    bad or over-eager value can never spawn an unbounded fan of workers.
    """
    raw = os.environ.get("SONDER_SPECULATION_SLOTS", "").strip()
    try:
        n = int(raw) if raw else _DEFAULT_SLOTS
    except ValueError:
        n = _DEFAULT_SLOTS
    return max(1, min(_MAX_SLOTS, n))


def predictor_path() -> Path:
    override = os.environ.get("SONDER_BRANCH_PREDICTOR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(sonder_paths.state_path("branch_predictor.json"))


@dataclass
class _Table:
    """One next-token frequency table keyed by a discrete state."""

    counts: dict = field(default_factory=dict)

    def observe(self, state: str, token: str) -> None:
        bucket = self.counts.setdefault(state, {})
        bucket[token] = bucket.get(token, 0) + 1

    def predict(self, state: str) -> tuple[str, float] | None:
        bucket = self.counts.get(state)
        if not bucket:
            return None
        total = sum(bucket.values())
        token, hits = max(bucket.items(), key=lambda kv: kv[1])
        return token, hits / total if total else 0.0


class BranchPredictor:
    """Two-level predictor: opener tier per request kind, next tool per state.

    The design mirrors a CPU's history-indexed predictor: the "global
    history" is the sequence of tools already used this run, hashed into a
    compact state so the next-tool table conditions on recent behavior, not
    just the immediately preceding tool.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or predictor_path()
        self._lock = threading.RLock()
        self._openers = _Table()   # request-kind -> first tier/tool
        self._transitions = _Table()  # loop-state -> next tool
        self._loaded = False
        self.hits = 0
        self.misses = 0
        self.speculations = 0
        self.squashes = 0
        # Cost model (learned per machine, persisted across runs).
        self.ewma_decision_s = 0.0  # model time to decide the next tool
        self.ewma_tool_s = 0.0      # wall time of a speculated read-only tool
        # Per-tool latency table: some read-only tools (a wide recursive scan)
        # are seconds while others (a status probe) are microseconds, so a
        # single global figure under- or over-values speculation depending on
        # which tool is predicted.  Keyed by tool name, learned alongside the
        # global figure, and used by expected_saving_seconds when available.
        self.ewma_tool_by_name: dict[str, float] = {}
        self.saved_s = 0.0          # wall time actually hidden by retired specs

    # -- persistence -------------------------------------------------------

    def load(self) -> "BranchPredictor":
        with self._lock:
            if self._loaded:
                return self
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if raw.get("version") == _PREDICTOR_VERSION:
                    self._openers.counts = raw.get("openers", {})
                    self._transitions.counts = raw.get("transitions", {})
                    cost = raw.get("cost", {})
                    try:
                        self.ewma_decision_s = max(
                            0.0, float(cost.get("ewma_decision_s", 0.0)))
                        self.ewma_tool_s = max(
                            0.0, float(cost.get("ewma_tool_s", 0.0)))
                    except (TypeError, ValueError):
                        self.ewma_decision_s = 0.0
                        self.ewma_tool_s = 0.0
                    # Per-tool table is optional so files written before this
                    # existed load cleanly; drop any malformed entry silently.
                    per_tool = cost.get("ewma_tool_by_name", {})
                    table: dict[str, float] = {}
                    if isinstance(per_tool, dict):
                        for name, value in per_tool.items():
                            try:
                                seconds = float(value)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(name, str) and seconds > 0.0:
                                table[name] = seconds
                    self.ewma_tool_by_name = table
            except (OSError, ValueError, TypeError):
                pass
            self._loaded = True
            return self

    def save(self) -> None:
        with self._lock:
            self._prune_locked()
            payload = {
                "version": _PREDICTOR_VERSION,
                "openers": self._openers.counts,
                "transitions": self._transitions.counts,
                "cost": {
                    "ewma_decision_s": round(self.ewma_decision_s, 6),
                    "ewma_tool_s": round(self.ewma_tool_s, 6),
                    "ewma_tool_by_name": {
                        name: round(value, 6)
                        for name, value in self.ewma_tool_by_name.items()
                    },
                },
            }
        try:
            tmp = self._path.with_name(
                "%s.tmp-%d" % (self._path.name, os.getpid())
            )
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _prune_locked(self) -> None:
        # Bound growth: keep the most-populated transition states.
        if len(self._transitions.counts) <= _MAX_TRANSITIONS:
            return
        ranked = sorted(
            self._transitions.counts.items(),
            key=lambda kv: sum(kv[1].values()),
            reverse=True,
        )
        self._transitions.counts = dict(ranked[:_MAX_TRANSITIONS])

    # -- state hashing -----------------------------------------------------

    @staticmethod
    def loop_state(used_tools: tuple[str, ...], last_tool: str | None,
                   step: int) -> str:
        """Compact discrete state for the next-tool table.

        Conditioned on the last tool plus the set of tools already used and
        whether inspection has happened — enough signal to predict the next
        move without exploding the table with raw argument values.
        """
        recent = last_tool or "<start>"
        seen = ",".join(sorted(set(used_tools))[:6])
        phase = "early" if step <= 2 else "mid" if step <= 4 else "late"
        return "%s|%s|%s" % (phase, recent, seen)

    @staticmethod
    def request_kind(prompt: str) -> str:
        """Bucket a request into a coarse kind for opener prediction."""
        low = (prompt or "").lower()
        if any(k in low for k in ("test", "pytest", "assert")):
            return "test"
        if any(k in low for k in ("fix", "bug", "error", "traceback")):
            return "debug"
        if any(k in low for k in ("write", "create", "implement", "add ")):
            return "author"
        if any(k in low for k in ("explain", "what", "why", "how", "?")):
            return "explain"
        if any(k in low for k in ("find", "search", "where", "list")):
            return "search"
        return "general"

    # -- learning ----------------------------------------------------------

    def record_opener(self, prompt: str, tier: str) -> None:
        with self._lock:
            self._openers.observe(self.request_kind(prompt), tier)

    def record_transition(
        self, state: str, tool_name: str
    ) -> None:
        if not tool_name:
            return
        with self._lock:
            self._transitions.observe(state, tool_name)

    # -- prediction --------------------------------------------------------

    def predict_opener(self, prompt: str) -> tuple[str, float] | None:
        with self._lock:
            return self._openers.predict(self.request_kind(prompt))

    def predict_next_tool(self, state: str) -> tuple[str, float] | None:
        with self._lock:
            prediction = self._transitions.predict(state)
        if prediction is None:
            return None
        tool, confidence = prediction
        if confidence < _MIN_CONFIDENCE:
            return None
        return tool, confidence

    def speculatable(self, tool_name: str) -> bool:
        return tool_name in SPECULATABLE_TOOLS

    # -- accounting --------------------------------------------------------

    def note_hit(self) -> None:
        with self._lock:
            self.hits += 1

    def note_miss(self) -> None:
        with self._lock:
            self.misses += 1

    def note_speculation(self) -> None:
        with self._lock:
            self.speculations += 1

    def note_squash(self) -> None:
        with self._lock:
            self.squashes += 1

    # -- cost model --------------------------------------------------------

    @staticmethod
    def _ewma(prev: float, sample: float) -> float:
        if sample <= 0.0:
            return prev
        if prev <= 0.0:
            return sample
        return (1.0 - _SPEC_EWMA_ALPHA) * prev + _SPEC_EWMA_ALPHA * sample

    def observe_decision_latency(self, seconds: float) -> None:
        """Record how long the model took to decide the next tool."""
        with self._lock:
            self.ewma_decision_s = self._ewma(self.ewma_decision_s, seconds)

    def observe_tool_latency(
        self, seconds: float, tool_name: str | None = None
    ) -> None:
        """Record the wall time of a speculated read-only tool call.

        Always folds into the global figure; when ``tool_name`` is given it
        also folds into that tool's own EWMA so per-tool latencies diverge
        from the global average as evidence accumulates.  ``tool_name`` is
        optional so existing callers that pass only a duration keep working.
        """
        with self._lock:
            self.ewma_tool_s = self._ewma(self.ewma_tool_s, seconds)
            if tool_name:
                self.ewma_tool_by_name[tool_name] = self._ewma(
                    self.ewma_tool_by_name.get(tool_name, 0.0), seconds
                )

    def note_saved(self, seconds: float) -> None:
        """Accumulate wall time actually hidden by a retired speculation."""
        if seconds <= 0.0:
            return
        with self._lock:
            self.saved_s += seconds

    def expected_saving_seconds(
        self, confidence: float, tool_name: str | None = None
    ) -> float:
        """Expected wall time a correct speculation would hide this step.

        The hideable overlap is the shorter of the decision and tool
        latencies, discounted by how likely the prediction is to be the
        branch actually taken.  Returns a negative sentinel while the cost
        model is still cold (no measured latencies yet).

        When ``tool_name`` names a tool with its own measured latency, that
        per-tool figure is used in place of the global average — a slow scan
        is judged worth hiding even when the average tool is instant, and a
        fast probe is judged not worth it even when the average is slow.
        Falls back to the global figure for unknown tools, so the original
        single-argument call is unchanged.
        """
        with self._lock:
            dec = self.ewma_decision_s
            tool = self.ewma_tool_s
            if tool_name:
                per_tool = self.ewma_tool_by_name.get(tool_name, 0.0)
                if per_tool > 0.0:
                    tool = per_tool
        if dec <= 0.0 or tool <= 0.0:
            return -1.0
        conf = max(0.0, min(1.0, confidence))
        return min(dec, tool) * conf

    def should_speculate(
        self, confidence: float, tool_name: str | None = None
    ) -> bool:
        """Gate a speculation on its expected payoff on *this* machine.

        A bounded warmup lets the predictor measure real latencies before it
        starts refusing; after that, speculation runs only when the expected
        hidden wall time clears the configured floor.  This is what keeps it
        dormant on a CPU (millisecond tools) and active on serious hardware
        (slow model + slow tools) without a deployment-specific setting.
        """
        expected = self.expected_saving_seconds(confidence, tool_name)
        if expected < 0.0:
            with self._lock:
                return self.speculations < _SPEC_WARMUP
        return expected >= min_saving_seconds()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "predictions": total,
                "hits": self.hits,
                "misses": self.misses,
                "accuracy": (self.hits / total) if total else 0.0,
                "speculations": self.speculations,
                "squashes": self.squashes,
                "speculation_hit_rate": (
                    (self.speculations - self.squashes) / self.speculations
                    if self.speculations else 0.0
                ),
                "transition_states": len(self._transitions.counts),
                "opener_kinds": len(self._openers.counts),
                "ewma_decision_s": round(self.ewma_decision_s, 4),
                "ewma_tool_s": round(self.ewma_tool_s, 4),
                "tool_latency_tools": len(self.ewma_tool_by_name),
                "saved_s": round(self.saved_s, 4),
            }


@dataclass
class SpeculativeResult:
    tool_name: str
    call_signature: str
    observation: str
    ok: bool
    started: float
    finished: float

    @property
    def wall_seconds(self) -> float:
        return max(0.0, self.finished - self.started)


class SpeculationEngine:
    """Runs predicted read-only tool calls in workers while the model
    generates, and retires or squashes them once the real decision lands.

    Analogous to a CPU issuing speculative loads past an unresolved branch
    into a small reorder buffer: each result sits in its own slot and is only
    committed (used) if the branch resolves the way that slot predicted;
    otherwise it is squashed.

    The buffer holds up to N slots (``SONDER_SPECULATION_SLOTS``, clamped to
    ``[1, _MAX_SLOTS]``, default 1).  With a single slot the engine is
    byte-for-byte equivalent to the original single-in-flight design: one
    speculation at a time, ``resolve`` retires it on a signature match and
    squashes it on a miss, ``discard`` flushes it.  With more slots several
    independent read-only speculations run at once and ``resolve`` retires
    whichever slot matches the committed branch, leaving the others in flight.
    """

    def __init__(self, predictor: BranchPredictor, dispatch, *, enabled=True,
                 slots: int | None = None):
        self._predictor = predictor
        self._dispatch = dispatch  # (tool_name, args) -> (observation, ok)
        self._enabled = enabled
        # Resolve the slot count once, at construction, so a live env change
        # can never resize the buffer under an in-flight step.  A single slot
        # preserves the original semantics exactly.
        if slots is None:
            self._max_slots = speculation_slots()
        else:
            self._max_slots = max(1, min(_MAX_SLOTS, int(slots)))
        # Each slot is a dict {tool_name, call_signature, result, thread}.
        # A list preserves issue order so a mispredict squashes the stalest.
        self._slots: list[dict] = []
        # A small read-through cache keeps a completed, squashed *read-only*
        # result available when a model takes the same branch one turn later.
        # Listing order and model read order need not agree; without this a
        # harmless prefetch of ``gamma`` followed by a real ``beta`` read can
        # dispatch ``gamma`` a second time on the next turn.  It is bounded by
        # the reorder buffer size and never contains a mutating tool.
        self._retired_cache: dict[str, SpeculativeResult] = {}
        self._lock = threading.Lock()

    def _cache_result(self, result: SpeculativeResult | None) -> None:
        if result is None or not result.ok:
            return
        with self._lock:
            self._retired_cache[result.call_signature] = result
            while len(self._retired_cache) > self._max_slots:
                self._retired_cache.pop(next(iter(self._retired_cache)))

    def begin(self, tool_name: str, call_signature: str, args: dict) -> bool:
        """Launch a speculative read-only call. Returns True if issued.

        Issues while a free slot exists; refuses (returns False) when the
        buffer is full, when the engine is disabled, or when the tool is not
        on the read-only allowlist (mutating tools are never speculated).
        """
        if not self._enabled or not self._predictor.speculatable(tool_name):
            return False
        with self._lock:
            # The result is already available for retirement.  Do not issue a
            # duplicate read while the model decides whether to use it.
            if call_signature in self._retired_cache:
                return False
            if len(self._slots) >= self._max_slots:
                return False  # buffer full; back-pressure like a full ROB
            slot: dict = {
                "tool_name": tool_name,
                "call_signature": call_signature,
                "result": None,
                "thread": None,
            }

            def _work(slot=slot):
                started = time.monotonic()
                try:
                    observation, ok = self._dispatch(tool_name, args)
                except Exception as exc:  # a bad speculation must never raise
                    observation, ok = ("speculation error: %s" % exc), False
                finished = time.monotonic()
                with self._lock:
                    slot["result"] = SpeculativeResult(
                        tool_name=tool_name,
                        call_signature=call_signature,
                        observation=str(observation),
                        ok=bool(ok),
                        started=started,
                        finished=finished,
                    )

            thread = threading.Thread(
                target=_work, daemon=True, name="sonder-speculation"
            )
            slot["thread"] = thread
            self._slots.append(slot)
        self._predictor.note_speculation()
        thread.start()
        return True

    def resolve(self, real_signature: str) -> SpeculativeResult | None:
        """Retire the buffered result whose slot matches the committed branch.

        Scans the buffer for a slot whose call signature equals the real one.
        On a match that slot is joined and its result returned (latency
        hidden), leaving any other slots untouched and still in flight.  On a
        miss the stalest outstanding slot is squashed (only that one, unlike
        :meth:`discard` which flushes all) so a repeatedly mispredicting
        buffer cannot grow unbounded.  With a single slot this is exactly the
        original retire-on-match / squash-on-miss behavior.
        """
        with self._lock:
            match = None
            for slot in self._slots:
                if slot.get("call_signature") == real_signature:
                    match = slot
                    break
            if match is not None:
                self._slots.remove(match)
                target, hit = match, True
                cached = None
            elif real_signature in self._retired_cache:
                cached = self._retired_cache.pop(real_signature)
                target, hit = None, True
            elif self._slots:
                target, hit = self._slots.pop(0), False
                cached = None
            else:
                return None  # nothing in flight: idle, nothing to squash
        if cached is not None:
            return cached
        thread = target.get("thread")
        if thread is not None:
            thread.join(timeout=30)
        if hit:
            with self._lock:
                result = target.get("result")
            if result is not None:
                return result
            # Signature matched but the worker never produced a result (e.g.
            # a join timeout): treat it as a squash rather than a retirement.
        with self._lock:
            result = target.get("result")
        self._cache_result(result)
        self._predictor.note_squash()
        return None

    def discard(self) -> None:
        """Squash every outstanding speculation without retiring any.

        Called when a step resolves down a path that never consumes the
        speculative buffer (the model returned final, hit a cache, or chose a
        tool no slot predicted). Joins each worker so no orphan thread
        survives, and squashes once per slot that was in flight.
        """
        with self._lock:
            slots = self._slots
            self._slots = []
        for slot in slots:
            thread = slot.get("thread")
            if thread is not None:
                thread.join(timeout=30)
            with self._lock:
                result = slot.get("result")
            self._cache_result(result)
        for _ in slots:
            self._predictor.note_squash()

    @property
    def busy(self) -> bool:
        with self._lock:
            return len(self._slots) > 0

    @property
    def inflight(self) -> int:
        """Number of speculations currently buffered (0..N)."""
        with self._lock:
            return len(self._slots)

    @property
    def max_slots(self) -> int:
        """The reorder-buffer capacity this engine was constructed with."""
        return self._max_slots


# ---------------------------------------------------------------------------
# File prefetcher — argument-level prediction (hardware-prefetcher analog)
# ---------------------------------------------------------------------------

# Observation formats that reveal an ordered file listing.
_LISTING_LINE_PATTERNS = (
    re.compile(r"^\s*file\s+(?P<path>\S+)\s+\(\d+ bytes\)", re.MULTILINE),
    re.compile(r"^\s*\[F\]\s+(?P<path>\S+)\s+\(\d+ bytes\)", re.MULTILINE),
    re.compile(r"^\s{2,}\d+\s+(?P<path>\S+\.\w{1,8})\s*$", re.MULTILINE),
)

_LISTING_TOOLS = frozenset({
    "file_find", "directory_tree", "workspace_inventory", "script_search",
})
_READ_TOOLS = frozenset({"file_read", "file_read_range"})
_PREFETCH_MAX_PATHS = 64


class FilePrefetcher:
    """Predict the next file the agent will read, like a stream prefetcher.

    A hardware prefetcher watches an access pattern (sequential cache lines)
    and fetches ahead. Agents show the same pattern one level up: they list
    files, then read them roughly in listing order. This tracker remembers
    the most recent observed listing, watches which entries get read, and —
    once at least one read has confirmed the stream — predicts the next
    unread entry so the host can speculatively execute that ``file_read``
    while the model is still deciding.

    Purely advisory and read-only, exactly like the rest of the speculation
    machinery: a wrong guess is a squashed read, never a behavior change.
    """

    def __init__(self, max_paths: int = _PREFETCH_MAX_PATHS) -> None:
        self._max_paths = max_paths
        self._listing: list[str] = []
        self._consumed: set[str] = set()
        self._stream_confirmed = False
        self._lock = threading.Lock()

    @staticmethod
    def extract_paths(observation: str) -> list[str]:
        seen: list[str] = []
        for pattern in _LISTING_LINE_PATTERNS:
            for match in pattern.finditer(observation or ""):
                path = match.group("path").strip()
                if path and path not in seen:
                    seen.append(path)
        return seen

    def observe(self, tool_name: str, args: dict, observation: str,
                *, ok: bool = True) -> None:
        if not ok:
            return
        if tool_name in _LISTING_TOOLS:
            paths = self.extract_paths(observation)[: self._max_paths]
            if paths:
                with self._lock:
                    self._listing = paths
                    self._consumed = set()
                    self._stream_confirmed = False
            return
        if tool_name in _READ_TOOLS:
            path = str((args or {}).get("path", "")).strip()
            if not path:
                return
            with self._lock:
                for candidate in self._listing:
                    if candidate == path or candidate.endswith("/" + path) \
                            or path.endswith("/" + candidate):
                        self._consumed.add(candidate)
                        # One read from the listing confirms the stream.
                        self._stream_confirmed = True
                        break

    def predict_read(self) -> dict | None:
        """Next ``file_read`` args, or None when no stream is confirmed."""
        with self._lock:
            if not self._stream_confirmed:
                return None
            for candidate in self._listing:
                if candidate not in self._consumed:
                    # Claim it so the same path is not predicted twice while
                    # its speculation is still in flight. Args mirror the
                    # model's canonical minimal call ({"path": ...}) so the
                    # call signatures can actually match at retirement.
                    self._consumed.add(candidate)
                    return {"path": candidate}
        return None

    def reset(self) -> None:
        with self._lock:
            self._listing = []
            self._consumed = set()
            self._stream_confirmed = False


_default: BranchPredictor | None = None
_default_lock = threading.Lock()


def default_predictor() -> BranchPredictor:
    global _default
    with _default_lock:
        if _default is None:
            _default = BranchPredictor().load()
        return _default


def speculation_enabled() -> bool:
    return os.environ.get("SONDER_SPECULATION", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def reset_for_tests() -> None:
    global _default
    with _default_lock:
        _default = None
