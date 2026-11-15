"""Schema-enforced generation on the normal ModelGateway path.

``generate_structured`` is not a second client.  It sends the ordinary
``ModelRequest`` through whatever ``ModelGateway`` the caller already holds,
with the schema added as the ``schema`` request option.  A gateway that can
constrain decoding (Ollama, as its ``format`` field) does so; one that cannot
(Sonder Inference v1 rejects ``response_format``) never sees the option act, and
the reply is checked here either way.  Constraining the decoder is a claim about
the backend; the check on the returned text is the evidence.

A schema using a keyword the check cannot evaluate raises ``InvalidInput``
before any model call: validation never reports success on unchecked rules.

On a failed check the call is retried (default once): the original prompt and
the rejected reply move into history (user, assistant) and the correction
becomes the new prompt, so gateways that place history before the prompt send
strictly alternating turns.  Exhausted retries return a
typed failure.  Gateway errors (timeouts, consent, outages) are not validation
failures and propagate unchanged.

State: stateless; every call is independent.  Thread affinity: none.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .context import OperationContext
from .ports.model_gateway import ModelGateway, ModelRequest
from .security.prompt_provenance import ProvenanceError
from ..domain.common.errors import InvalidInput
from ..domain.json_reply import check_reply, unsupported_keywords

MAX_RETRIES = 3
_ERROR_LIMIT = 8


@dataclass(frozen=True)
class StructuredAttempt:
    """One gateway call: what was served, what it cost, why it was rejected."""

    text: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class StructuredFailure:
    """Why the last attempt was rejected; ``kind`` is ``invalid_json``,
    ``schema_violation`` or ``retry_unavailable`` (the request could not be
    extended with the correction turns)."""

    kind: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class StructuredResult:
    value: object
    failure: StructuredFailure | None
    attempts: tuple[StructuredAttempt, ...]
    model: str
    tier: str
    tokens_in: int | None
    tokens_out: int | None
    # Always empty on a returned result (unsupported schemas raise before any
    # call); kept so callers can assert nothing went unchecked.
    unchecked: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.failure is None


def _sum(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _correction(errors: tuple[str, ...]) -> str:
    listed = "; ".join(errors[:_ERROR_LIMIT])
    return (
        "Your previous reply was rejected: %s. Reply again with only JSON "
        "that matches the required schema." % listed
    )


def generate_structured(
    gateway: ModelGateway,
    request: ModelRequest,
    context: OperationContext,
    schema: dict,
    *,
    retries: int = 1,
) -> StructuredResult:
    if not isinstance(schema, dict):
        raise InvalidInput("structured output schema must be a JSON object")
    gaps = unsupported_keywords(schema)
    if gaps:
        raise InvalidInput(
            "structured output schema uses keywords that cannot be validated: %s"
            % ", ".join(gaps)
        )
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise InvalidInput("structured output retries must be an integer from 0 to %d" % MAX_RETRIES)
    options = dict(request.options or {})
    options["schema"] = schema
    current = replace(request, options=options)
    attempts: list[StructuredAttempt] = []
    failure: StructuredFailure | None = None
    value: object = None
    unchecked: tuple[str, ...] = ()
    response = None
    for attempt in range(retries + 1):
        response = gateway.generate(current, context)
        checked = check_reply(response.text, schema)
        unchecked = checked.unchecked
        attempts.append(StructuredAttempt(
            text=response.text, model=response.model,
            tokens_in=response.tokens_in, tokens_out=response.tokens_out,
            errors=checked.errors,
        ))
        if checked.ok:
            failure, value = None, checked.value
            break
        failure = StructuredFailure(checked.kind, checked.errors)
        if attempt == retries:
            break
        try:
            current = replace(
                current,
                history=(
                    *current.history,
                    {"role": "user", "content": current.prompt},
                    {"role": "assistant", "content": response.text},
                ),
                prompt=_correction(checked.errors),
                # These describe the original request, not the retry.
                prefix_manifest=None,
                replay_manifest=None,
                prefix_cache_observation=None,
            )
        except ProvenanceError:
            failure = StructuredFailure("retry_unavailable", checked.errors)
            break
    return StructuredResult(
        value=value, failure=failure, attempts=tuple(attempts),
        model=response.model, tier=response.tier,
        tokens_in=_sum([a.tokens_in for a in attempts]),
        tokens_out=_sum([a.tokens_out for a in attempts]),
        unchecked=unchecked,
    )
