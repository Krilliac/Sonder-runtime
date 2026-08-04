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
    """Runs a predicted read-only tool call in a worker while the model
    generates, and retires or squashes it once the real decision lands.

    Analogous to a CPU issuing a speculative load past an unresolved
    branch: the result sits in a buffer and is only committed (used) if the
    branch resolves the way it was predicted; otherwise it is squashed.
    """

    def __init__(self, predictor: BranchPredictor, dispatch, *, enabled=True):
        self._predictor = predictor
        self._dispatch = dispatch  # (tool_name, args) -> (observation, ok)
        self._enabled = enabled
        self._pending: dict = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def begin(self, tool_name: str, call_signature: str, args: dict) -> bool:
        """Launch a speculative read-only call. Returns True if issued."""
        if not self._enabled or not self._predictor.speculatable(tool_name):
            return False
        with self._lock:
            if self._thread is not None:
                return False  # one in flight at a time, like a small buffer
            self._pending = {
                "tool_name": tool_name,
                "call_signature": call_signature,
                "result": None,
            }

            def _work():
                started = time.monotonic()
                try:
                    observation, ok = self._dispatch(tool_name, args)
                except Exception as exc:  # a bad speculation must never raise
                    observation, ok = ("speculation error: %s" % exc), False
                finished = time.monotonic()
                with self._lock:
                    if self._pending:
                        self._pending["result"] = SpeculativeResult(
                            tool_name=tool_name,
                            call_signature=call_signature,
                            observation=str(observation),
                            ok=bool(ok),
                            started=started,
                            finished=finished,
                        )

            self._thread = threading.Thread(
                target=_work, daemon=True, name="sonder-speculation"
            )
        self._predictor.note_speculation()
        self._thread.start()
        return True

    def resolve(self, real_signature: str) -> SpeculativeResult | None:
        """Retire the speculation if it matches the committed branch.

        A match returns the buffered result (latency hidden); a mismatch
        squashes it.  Either way the engine is left idle for the next step.
        """
        with self._lock:
            pending = self._pending
            thread = self._thread
        if thread is None or not pending:
            return None
        thread.join(timeout=30)
        with self._lock:
            result = self._pending.get("result")
            hit = bool(result) and pending.get("call_signature") == real_signature
            self._pending = {}
            self._thread = None
        if hit:
            return result
        self._predictor.note_squash()
        return None

    def discard(self) -> None:
        """Squash any outstanding speculation without retiring it.

        Called when a step resolves down a path that never consumes the
        speculative buffer (the model returned final, hit a cache, or chose
        a different tool). Joins the worker so no orphan thread survives.
        """
        with self._lock:
            thread = self._thread
            had_pending = bool(self._pending)
        if thread is not None:
            thread.join(timeout=30)
        with self._lock:
            self._pending = {}
            self._thread = None
        if had_pending:
            self._predictor.note_squash()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None


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
