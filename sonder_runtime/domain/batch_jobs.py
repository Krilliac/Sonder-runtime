"""Pure batch-job model: specs, strict templates, ordering, progress and token rollups.

A batch job is N items run through K stages.  Each (item, stage) pair is one
*step*, executed elsewhere as a single-model durable fanout child run.  This
module owns only the decisions that need no I/O: validating a spec, rendering a
prompt from an item and the answers of upstream stages, choosing the next
runnable step, deriving ``blocked`` steps, and reporting progress, ETA and
token totals.  It imports nothing outside ``domain`` and the standard library.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.model_usage import PROVIDER_TOKEN_SOURCES

# Failure classes that point at the backend rather than at one item.
BREAKER_FAILURE_CLASSES = frozenset(("unavailable", "transport", "timeout"))

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STAGE_REF = "stage."


class JobStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ACTIVE_JOB_STATUSES = frozenset((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED))


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    ANSWERED = "answered"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


# A downstream step cannot run behind any of these.
UNSATISFIED_STEP_STATUSES = frozenset(
    (StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.UNKNOWN, StepStatus.BLOCKED)
)
DONE_STEP_STATUSES = frozenset((StepStatus.ANSWERED,)) | UNSATISFIED_STEP_STATUSES


class PauseReason(str, Enum):
    CONSECUTIVE_FAILURES = "consecutive_failures"


class StepReason(str, Enum):
    """Closed vocabulary for a step the runner failed without a model call."""

    PROMPT_TOO_LARGE = "prompt_too_large"
    UPSTREAM_TRUNCATED = "upstream_truncated"
    UPSTREAM_NOT_ANSWERED = "upstream_not_answered"


@dataclass(frozen=True)
class Limits:
    """Validation bounds; the caller builds them from ``[batch_jobs]``."""

    max_items_per_job: int = 5000
    max_stages: int = 8
    max_items_per_add: int = 500
    max_item_input_chars: int = 32000
    max_rendered_prompt_chars: int = 32000
    default_item_timeout_seconds: int = 300
    max_item_timeout_seconds: int = 900


@dataclass(frozen=True)
class StageSpec:
    name: str
    model: str
    prompt_template: str
    system: str = ""
    num_predict: int = 512
    num_ctx: int = 4096
    think: bool | None = None
    temperature: float = 0.2
    timeout_s: int = 300
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemSpec:
    key: str
    fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    name: str
    fields: tuple[str, ...]
    stages: tuple[StageSpec, ...]

    def stage(self, name: str) -> StageSpec:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise InvalidInput("unknown stage")


@dataclass(frozen=True)
class StepState:
    """What is known about one step: a child run's single result row."""

    stage: str
    item_key: str
    status: StepStatus = StepStatus.PENDING
    run_id: str = ""
    requested_model: str = ""
    served_model: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    token_source: str = ""
    elapsed_ms: int | None = None
    attempts: int = 0
    finished_ts: float | None = None
    failure_class: str | None = None
    reason: str = ""
    answer_truncated: bool = False
    answer_truncation_known: bool = False


def _plain_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidInput("%s must be an integer in %d..%d" % (name, low, high))
    return value


def _plain_text(value: object, name: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > limit:
        raise InvalidInput("%s must be a string of at most %d characters" % (name, limit))
    if _CONTROL.search(value):
        raise InvalidInput("%s contains control characters" % name)
    return value


def template_references(template: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(field names, stage names)`` a template uses, rejecting any other placeholder."""
    fields, stages = [], []
    for match in _PLACEHOLDER.finditer(template):
        token = match.group(1)
        if token.startswith(_STAGE_REF):
            name = token[len(_STAGE_REF):]
            if not _NAME.fullmatch(name):
                raise InvalidInput("malformed stage reference in template")
            if name not in stages:
                stages.append(name)
        elif _FIELD.fullmatch(token):
            if token not in fields:
                fields.append(token)
        else:
            raise InvalidInput("malformed placeholder in template")
    return tuple(fields), tuple(stages)


def _ancestors(stage_index: int, deps: Sequence[tuple[str, ...]], names: Sequence[str]) -> set[str]:
    """Transitive closure of ``depends_on`` for the stage at ``stage_index``."""
    seen: set[str] = set()
    pending = list(deps[stage_index])
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(deps[names.index(name)])
    return seen


def validate_spec(raw: object, limits: Limits, *, is_cloud_model: Callable[[str], bool]) -> JobSpec:
    """Validate a caller-supplied job spec; raises ``InvalidInput`` with a precise reason.

    ``raw`` is ``{"name", "fields": [...], "stages": [{name, model, prompt, ...}]}``.
    Every placeholder must name a declared field or an *ancestor* stage; a
    forward or unrelated stage reference is refused so the answer a template
    reads is guaranteed to exist before the step runs.  Cloud models are
    refused: a batch is local-only.
    """
    if not isinstance(raw, Mapping):
        raise InvalidInput("job spec must be an object")
    name = _plain_text(raw.get("name", ""), "name", limit=120, allow_empty=True)
    declared = raw.get("fields", ())
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Iterable):
        raise InvalidInput("fields must be a list of names")
    fields: list[str] = []
    for value in declared:
        if not isinstance(value, str) or not _FIELD.fullmatch(value):
            raise InvalidInput("field names must match [A-Za-z_][A-Za-z0-9_]{0,63}")
        if value in fields:
            raise InvalidInput("duplicate field name")
        fields.append(value)
    stages_raw = raw.get("stages")
    if isinstance(stages_raw, (str, bytes)) or not isinstance(stages_raw, Sequence) or not stages_raw:
        raise InvalidInput("a job needs at least one stage")
    if len(stages_raw) > limits.max_stages:
        raise InvalidInput("a job has at most %d stages" % limits.max_stages)
    names = [str(stage.get("name", "")) if isinstance(stage, Mapping) else "" for stage in stages_raw]
    stages: list[StageSpec] = []
    resolved_deps: list[tuple[str, ...]] = []
    for index, stage in enumerate(stages_raw):
        if not isinstance(stage, Mapping):
            raise InvalidInput("each stage must be an object")
        stage_name = names[index]
        if not _NAME.fullmatch(stage_name):
            raise InvalidInput("stage name must match [A-Za-z][A-Za-z0-9_-]{0,31}")
        if names.count(stage_name) > 1:
            raise InvalidInput("duplicate stage name")
        model = _plain_text(stage.get("model", ""), "model", limit=200).strip()
        if is_cloud_model(model):
            raise InvalidInput("batch jobs run local models only; refused cloud model for stage " + stage_name)
        template = _plain_text(stage.get("prompt", ""), "prompt", limit=limits.max_rendered_prompt_chars)
        system = _plain_text(stage.get("system", ""), "system", limit=limits.max_rendered_prompt_chars, allow_empty=True)
        used_fields, used_stages = template_references(template + "\n" + system)
        unknown = [value for value in used_fields if value not in fields]
        if unknown:
            raise InvalidInput("template uses undeclared field: " + unknown[0])
        for ref in used_stages:
            if ref not in names:
                raise InvalidInput("template references an unknown stage: " + ref)
            if names.index(ref) >= index:
                raise InvalidInput("template references a stage that is not earlier: " + ref)
        explicit = stage.get("depends_on")
        if explicit is None:
            deps = used_stages
        else:
            if isinstance(explicit, (str, bytes)) or not isinstance(explicit, Sequence):
                raise InvalidInput("depends_on must be a list of stage names")
            deps = tuple(dict.fromkeys(str(value) for value in explicit))
            for dep in deps:
                if dep not in names:
                    raise InvalidInput("depends_on names an unknown stage: " + dep)
                if names.index(dep) >= index:
                    raise InvalidInput("depends_on names a stage that is not earlier: " + dep)
        resolved_deps.append(deps)
        reachable = _ancestors(index, resolved_deps, names)
        for ref in used_stages:
            if ref not in reachable:
                raise InvalidInput("template references a stage that is not an ancestor: " + ref)
        timeout = stage.get("timeout_s", limits.default_item_timeout_seconds)
        think = stage.get("think")
        if think is not None and not isinstance(think, bool):
            raise InvalidInput("think must be a boolean")
        temperature = stage.get("temperature", 0.2)
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) \
                or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise InvalidInput("temperature must be a number in 0..2")
        stages.append(StageSpec(
            name=stage_name, model=model, prompt_template=template, system=system,
            num_predict=_plain_int(stage.get("num_predict", 512), "num_predict", 32, 4096),
            num_ctx=_plain_int(stage.get("num_ctx", 4096), "num_ctx", 512, 1_048_576),
            think=think, temperature=float(temperature),
            timeout_s=_plain_int(timeout, "timeout_s", 5, limits.max_item_timeout_seconds),
            depends_on=deps,
        ))
    return JobSpec(name=name.strip(), fields=tuple(fields), stages=tuple(stages))


def validate_items(raw: object, spec: JobSpec, limits: Limits, *, existing_keys: Iterable[str] = ()) -> tuple[ItemSpec, ...]:
    """Validate one page of items: ``[{"key": str, "fields": {name: str}}]``."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise InvalidInput("items must be a list")
    if len(raw) > limits.max_items_per_add:
        raise InvalidInput("at most %d items can be added at once" % limits.max_items_per_add)
    seen = set(existing_keys)
    items = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) - {"key", "fields"}:
            raise InvalidInput("each item must be an object with key and fields")
        key = _plain_text(entry.get("key", ""), "item key", limit=200)
        if key in seen:
            raise InvalidInput("duplicate item key: " + key)
        seen.add(key)
        values = entry.get("fields", {})
        if not isinstance(values, Mapping) or set(values) != set(spec.fields):
            raise InvalidInput("item fields must be exactly: " + ", ".join(spec.fields))
        total = 0
        for value in values.values():
            if not isinstance(value, str):
                raise InvalidInput("item field values must be strings")
            total += len(value)
        if total > limits.max_item_input_chars:
            raise InvalidInput("item input exceeds %d characters" % limits.max_item_input_chars)
        items.append(ItemSpec(key=key, fields=dict(values)))
    if len(seen) > limits.max_items_per_job:
        raise InvalidInput("a job has at most %d items" % limits.max_items_per_job)
    return tuple(items)


class PromptTooLarge(InvalidInput):
    """The rendered ``system`` + ``prompt`` would exceed the caller's character limit."""


def render_prompt(stage: StageSpec, item: ItemSpec, upstream: Mapping[str, str], *,
                  max_chars: int | None = None) -> tuple[str, str]:
    """Render ``(system, prompt)`` in one pass; substituted values are never re-scanned.

    With ``max_chars``, raises :class:`PromptTooLarge` as soon as the combined
    length would exceed it, before the oversized text is built: a template
    repeating one placeholder thousands of times would otherwise multiply a
    large field or upstream answer into a string of hundreds of millions of
    characters first.
    """
    used = 0

    def resolve(token: str) -> str:
        if token.startswith(_STAGE_REF):
            name = token[len(_STAGE_REF):]
            if name not in upstream:
                raise InvalidInput("upstream answer is not available: " + name)
            return upstream[name]
        if token not in item.fields:
            raise InvalidInput("item has no field: " + token)
        return item.fields[token]

    def render(template: str) -> str:
        parts: list[str] = []

        def add(piece: str) -> None:
            nonlocal used
            used += len(piece)
            if max_chars is not None and used > max_chars:
                raise PromptTooLarge("rendered prompt exceeds %d characters" % max_chars)
            parts.append(piece)

        last = 0
        for match in _PLACEHOLDER.finditer(template):
            add(template[last:match.start()])
            add(resolve(match.group(1)))
            last = match.end()
        add(template[last:])
        return "".join(parts)

    return render(stage.system), render(stage.prompt_template)


def effective_statuses(spec: JobSpec, item_keys: Sequence[str],
                       states: Mapping[tuple[str, str], StepState]) -> dict[tuple[str, str], StepStatus]:
    """Every step's status, with ``blocked`` derived behind an unsatisfied dependency.

    ``blocked`` is never stored: it follows from the upstream step and so can
    never go stale.  A step is blocked only while it has not run; a step with
    its own result keeps it.
    """
    result: dict[tuple[str, str], StepStatus] = {}
    for stage in spec.stages:
        for key in item_keys:
            raw = states.get((stage.name, key))
            status = raw.status if raw is not None else StepStatus.PENDING
            if status is StepStatus.PENDING and any(
                result[(dep, key)] in UNSATISFIED_STEP_STATUSES for dep in stage.depends_on
            ):
                status = StepStatus.BLOCKED
            result[(stage.name, key)] = status
    return result


def next_step(spec: JobSpec, item_keys: Sequence[str],
              effective: Mapping[tuple[str, str], StepStatus], *, order: str = "stage_major") -> tuple[str, str] | None:
    """The next ``(stage name, item key)`` that is pending with every dependency answered."""
    if order == "item_major":
        sequence = ((stage, key) for key in item_keys for stage in spec.stages)
    else:
        sequence = ((stage, key) for stage in spec.stages for key in item_keys)
    for stage, key in sequence:
        if effective[(stage.name, key)] is not StepStatus.PENDING:
            continue
        if all(effective[(dep, key)] is StepStatus.ANSWERED for dep in stage.depends_on):
            return stage.name, key
    return None


def is_finished(effective: Mapping[tuple[str, str], StepStatus]) -> bool:
    """No step is pending or running: nothing more can be dispatched."""
    return not any(status in (StepStatus.PENDING, StepStatus.RUNNING) for status in effective.values())


def counts(effective: Mapping[tuple[str, str], StepStatus]) -> dict[str, int]:
    tally = {status.value: 0 for status in StepStatus}
    for status in effective.values():
        tally[status.value] += 1
    return tally


def progress(spec: JobSpec, item_keys: Sequence[str], states: Mapping[tuple[str, str], StepState],
             *, segment_start: float | None, eta_window: int, eta_min_samples: int) -> dict[str, Any]:
    """Done/total, rate and ETA.  Blocked steps count as done (they will never run).

    The rate uses only completions inside the current execution segment (since
    the last start or resume), so time a job spent paused or interrupted never
    dilutes it.  With fewer than ``eta_min_samples`` such completions the rate
    and ETA are ``None`` with ``eta_basis`` ``insufficient_samples``: an ETA is
    never invented.
    """
    effective = effective_statuses(spec, item_keys, states)
    tally = counts(effective)
    total = len(effective)
    done = sum(tally[status.value] for status in DONE_STEP_STATUSES)
    remaining = total - done
    stamps = sorted(
        state.finished_ts for state in states.values()
        if state.finished_ts is not None and state.status in (StepStatus.ANSWERED, StepStatus.FAILED)
        and segment_start is not None and state.finished_ts >= segment_start
    )[-max(2, eta_window):]
    rate_per_min = eta_s = None
    basis = "insufficient_samples"
    if len(stamps) >= max(2, eta_min_samples) and stamps[-1] > stamps[0]:
        rate_per_sec = (len(stamps) - 1) / (stamps[-1] - stamps[0])
        rate_per_min = rate_per_sec * 60.0
        eta_s = remaining / rate_per_sec
        basis = "recent_window"
    elif remaining == 0 and total:
        eta_s, basis = 0.0, "complete"
    return {
        "total": total, "done": done, "remaining": remaining, **tally,
        "samples": len(stamps), "rate_per_min": rate_per_min, "eta_s": eta_s, "eta_basis": basis,
    }


def token_rollup(states: Iterable[StepState]) -> dict[str, int]:
    """Token totals with provider-counted and estimated counts kept apart.

    A step with no recorded tokens is *unknown*, never zero.  An estimate never
    lands in the provider-counted total.
    """
    provider_in = provider_out = estimated_in = estimated_out = 0
    estimated_rows = unknown_rows = 0
    for state in states:
        if state.tokens_in is None and state.tokens_out is None:
            if state.status is StepStatus.ANSWERED:
                unknown_rows += 1
            continue
        if state.token_source in PROVIDER_TOKEN_SOURCES:
            provider_in += state.tokens_in or 0
            provider_out += state.tokens_out or 0
        else:
            estimated_rows += 1
            estimated_in += state.tokens_in or 0
            estimated_out += state.tokens_out or 0
    return {
        "tokens_in_provider": provider_in, "tokens_out_provider": provider_out,
        "tokens_in_estimated": estimated_in, "tokens_out_estimated": estimated_out,
        "rows_with_estimated_tokens": estimated_rows, "rows_with_unknown_tokens": unknown_rows,
    }


def served_model_counts(states: Iterable[StepState]) -> dict[str, int]:
    """How many steps each model actually answered, so a substitution shows up."""
    counted: dict[str, int] = {}
    for state in states:
        if state.served_model:
            counted[state.served_model] = counted.get(state.served_model, 0) + 1
    return dict(sorted(counted.items()))


MAX_LISTED_STEPS = 200


def step_entry(state: StepState, status: StepStatus | None = None) -> dict[str, Any]:
    """A step as a receipt row: models, tokens and timing, never any content."""
    return {
        "stage": state.stage, "item_key": state.item_key, "status": (status or state.status).value,
        "requested_model": state.requested_model or None, "served_model": state.served_model or None,
        "tokens_in": state.tokens_in, "tokens_out": state.tokens_out,
        "token_source": state.token_source or None, "elapsed_ms": state.elapsed_ms,
        "attempts": state.attempts, "failure_class": state.failure_class, "reason": state.reason or None,
        "answer_truncated": state.answer_truncated if state.answer_truncation_known else None,
    }


def receipt_body(spec: JobSpec, item_keys: Sequence[str], states: Mapping[tuple[str, str], StepState],
                 *, segment_start: float | None, eta_window: int, eta_min_samples: int) -> dict[str, Any]:
    """Progress, token totals, per-stage rollups, failures and blocked steps for a job receipt."""
    effective = effective_statuses(spec, item_keys, states)
    stages = []
    for stage in spec.stages:
        own = [state for (name, _), state in states.items() if name == stage.name]
        stage_counts = {status.value: 0 for status in StepStatus}
        for (name, _), status in effective.items():
            if name == stage.name:
                stage_counts[status.value] += 1
        stages.append({
            "name": stage.name, "requested_model": stage.model, "counts": stage_counts,
            "served_models": served_model_counts(own), **token_rollup(own),
        })
    listed = [
        step_entry(state) for key in sorted(states) if (state := states[key]).status in (StepStatus.FAILED, StepStatus.UNKNOWN)
    ]
    blocked = [{"stage": name, "item_key": key} for (name, key), status in effective.items()
               if status is StepStatus.BLOCKED]
    return {
        "progress": progress(spec, item_keys, states, segment_start=segment_start,
                             eta_window=eta_window, eta_min_samples=eta_min_samples),
        "tokens": token_rollup(states.values()),
        "served_models": served_model_counts(states.values()),
        "stages": stages,
        "failures": listed[:MAX_LISTED_STEPS], "failures_truncated": len(listed) > MAX_LISTED_STEPS,
        "blocked": blocked[:MAX_LISTED_STEPS], "blocked_truncated": len(blocked) > MAX_LISTED_STEPS,
    }


def breaker_trips(consecutive: int, limit: int) -> bool:
    """Whether ``consecutive`` backend failures in a row reach the pause threshold."""
    return consecutive >= limit


def spec_to_dict(spec: JobSpec) -> dict[str, Any]:
    """Persistable form of an already validated spec."""
    return {
        "name": spec.name, "fields": list(spec.fields),
        "stages": [
            {
                "name": stage.name, "model": stage.model, "prompt": stage.prompt_template,
                "system": stage.system, "num_predict": stage.num_predict, "num_ctx": stage.num_ctx,
                "think": stage.think, "temperature": stage.temperature, "timeout_s": stage.timeout_s,
                "depends_on": list(stage.depends_on),
            }
            for stage in spec.stages
        ],
    }


def spec_from_dict(raw: Mapping[str, Any]) -> JobSpec:
    """Rebuild a spec this module persisted; the data is trusted, not re-validated."""
    return JobSpec(
        name=str(raw.get("name", "")),
        fields=tuple(raw.get("fields", ())),
        stages=tuple(
            StageSpec(
                name=stage["name"], model=stage["model"], prompt_template=stage["prompt"],
                system=stage.get("system", ""), num_predict=stage["num_predict"],
                num_ctx=stage["num_ctx"], think=stage.get("think"),
                temperature=stage["temperature"], timeout_s=stage["timeout_s"],
                depends_on=tuple(stage.get("depends_on", ())),
            )
            for stage in raw["stages"]
        ),
    )


def referenced_stages(stage: StageSpec) -> tuple[str, ...]:
    """Upstream stages whose answers the stage's templates read."""
    return template_references(stage.prompt_template + "\n" + stage.system)[1]
