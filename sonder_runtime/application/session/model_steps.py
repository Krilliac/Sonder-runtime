"""Durable model-step boundary for legacy generation callables.

The typed chat and agent services already admit a ``ModelRequest`` before
dispatching a provider.  A few compatibility surfaces still expose a
``generate(prompt, history)`` callable instead.  This module gives those
surfaces the same ownership rules without moving their routing, validation, or
error translation into the application layer:

* admission is deferred until a provider dispatch, so cache-only paths do not
  invent a model request;
* the request snapshot retains the caller's options, history, and any already
  validated provenance binding;
* effective provider attempts are captured by ``dispatch_provider`` in the
  surrounding scope; and
* legacy exceptions cross the boundary unchanged while only a stable domain
  error code is written to the session.

An absent ``session_id`` is an explicit compatibility opt-out.  This matters
for existing one-shot callers and keeps the boundary from silently creating a
shared transcript for work that did not ask for one.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ...domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    ConcurrencyConflict,
    Conflict,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    IntegrityFailure,
    InternalFailure,
    InvalidInput,
    MigrationRequired,
    NotFound,
    SonderError,
    Unauthenticated,
)
from ...domain.common.ids import new_id
from ..ports.model_gateway import ModelRequest
from .capture import SessionCaptureService
from .provider_attempts import (
    ProviderCaptureFailure,
    complete_scoped_provider_request,
    deferred_provider_request_scope,
)


_T = TypeVar("_T")
_HISTORY_UNSET = object()
_FAILURE_CODES = frozenset({
    error.code for error in (
        Cancelled,
        CapacityExceeded,
        ConcurrencyConflict,
        Conflict,
        DeadlineExceeded,
        DependencyUnavailable,
        Forbidden,
        IntegrityFailure,
        InternalFailure,
        InvalidInput,
        MigrationRequired,
        NotFound,
        Unauthenticated,
    )
})


def _session_key(value: object) -> str | None:
    """Return a non-empty session identity without normalizing its namespace."""
    if value is None:
        return None
    key = str(value).strip()
    return None if not key or key.casefold() == "none" else key


def _default_response_text(value: _T) -> str:
    """Select a textual response from a legacy generator result."""
    if isinstance(value, str):
        return value
    # A compatibility caller may return a small response object while keeping
    # the legacy callable shape.  Do not guess arbitrary attributes: the
    # caller must supply ``response_text`` for anything except plain text.
    raise TypeError("legacy model step must return text")


def _failure_code(error: BaseException, mapper: Callable[[BaseException], str] | None) -> str:
    """Choose an allowlisted durable code while retaining the live exception."""
    if mapper is not None:
        code = mapper(error)
    elif isinstance(error, SonderError):
        code = error.code
    else:
        code = InternalFailure.code
    code = str(code or InternalFailure.code)
    return code if code in _FAILURE_CODES else InternalFailure.code


def _admitted(scope: object, session_id: str):
    """Return the current scope's admission only when it belongs to us."""
    admission = getattr(scope, "admission", None)
    if not admission or len(admission) != 2:
        return None
    capture, pending = admission
    if getattr(pending, "session_id", None) != session_id:
        return None
    return capture, pending


def _record_failure(capture, pending, error: Exception, mapper) -> None:
    """Persist a logical failure, failing closed if that evidence cannot append."""
    if pending is None or isinstance(error, ProviderCaptureFailure):
        return
    try:
        capture.fail_request(
            pending,
            error_code=_failure_code(error, mapper),
        )
    except Exception as capture_error:
        raise IntegrityFailure("could not persist model failure") from capture_error


def run_model_step(
    invoke: Callable[[], _T],
    *,
    capture_factory: Callable[[], SessionCaptureService] | None,
    session_id: str | None,
    request: ModelRequest,
    user_message: str | None = None,
    turn_id: str | None = None,
    request_id: str | None = None,
    response_text: Callable[[_T], str] = _default_response_text,
    failure_code: Callable[[BaseException], str] | None = None,
) -> _T:
    """Run one legacy model call through the durable request boundary.

    ``invoke`` owns routing and provider transport.  It should call the
    existing ``dispatch_provider`` path when a provider is contacted; the
    deferred scope then records each effective attempt before transport.  A
    successful cache-only invocation is captured retrospectively as one
    request/response pair.  If the invocation raises, the original exception
    is re-raised after an allowlisted ``model.failed`` event is appended.

    The function intentionally does nothing beyond invoking ``invoke`` when
    ``session_id`` is absent.  This preserves explicit one-shot behavior and
    avoids constructing the application graph for unscoped legacy calls.
    """
    session = _session_key(session_id)
    if session is None:
        return invoke()
    if not isinstance(request, ModelRequest):
        raise TypeError("request must be ModelRequest")
    if capture_factory is None:
        raise ValueError("capture_factory is required for a captured model step")
    resolved_turn_id = str(turn_id or new_id("turn"))
    resolved_request_id = str(request_id or new_id("request"))

    def admit():
        capture = capture_factory()
        pending = capture.begin_request(
            session,
            resolved_turn_id,
            request,
            request_id=resolved_request_id,
            user_message=user_message,
        )
        return capture, pending

    with deferred_provider_request_scope(admit) as scope:
        try:
            result = invoke()
            text = response_text(result)
        except Exception as error:
            admitted = _admitted(scope, session)
            if admitted is not None:
                _record_failure(admitted[0], admitted[1], error, failure_code)
            raise

        admitted = _admitted(scope, session)
        if admitted is not None:
            # This completes the request owned by the deferred scope.  The
            # provider scope has already written the effective response, so a
            # capture failure here must remain an integrity failure and must
            # never be translated into a retryable model error.
            complete_scoped_provider_request(session, text)
        else:
            # No provider dispatch occurred (for example, a learning/cache
            # hit).  Preserve the request as a complete durable turn without
            # manufacturing provider evidence.
            capture_factory().capture_turn(
                session,
                resolved_turn_id,
                request,
                request_id=resolved_request_id,
                user_message=user_message,
                model_response=text,
            )
        return result


def wrap_model_generator(
    generator: Callable[..., str],
    *,
    capture_factory: Callable[[], SessionCaptureService] | None,
    session_id: str | None,
    tier: str,
    system: str = "",
    options: dict | None = None,
    options_factory: Callable[[str, tuple, Callable[..., str]], dict] | None = None,
    user_message: str | None = None,
    first_user_message: str | None = None,
    failure_code: Callable[[BaseException], str] | None = None,
):
    """Decorate a legacy ``generate(prompt, history)`` callable.

    The proxy forwards mutable generation metadata and output-budget overrides
    to the original callable.  Each invocation receives a fresh request and
    turn identity, which lets an interactive agent retain one durable session
    while keeping every decision or format-repair response independently
    replayable.
    """
    base_options = dict(options or {})
    first_message_pending = first_user_message

    class DurableGenerator:
        @property
        def num_predict_override(self):
            return getattr(generator, "num_predict_override", None)

        @num_predict_override.setter
        def num_predict_override(self, value):
            setattr(generator, "num_predict_override", value)

        def __call__(self, prompt, history=_HISTORY_UNSET):
            nonlocal first_message_pending
            history_values = tuple(
                () if history is _HISTORY_UNSET else (history or ())
            )
            if options_factory is None:
                request_options = dict(base_options)
            else:
                request_options = dict(
                    options_factory(prompt, history_values, generator) or {}
                )
            request = ModelRequest(
                prompt=prompt,
                tier=tier,
                system=system,
                history=history_values,
                options=request_options,
            )
            message = first_message_pending
            first_message_pending = None
            return run_model_step(
                lambda: (
                    generator(prompt)
                    if history is _HISTORY_UNSET
                    else generator(prompt, history)
                ),
                capture_factory=capture_factory,
                session_id=session_id,
                request=request,
                user_message=message if message is not None else user_message,
                response_text=_default_response_text,
                failure_code=failure_code,
            )

        def __getattr__(self, name):
            return getattr(generator, name)

    return DurableGenerator()


# A descriptive compatibility spelling for callers that want to make the
# legacy nature of the callable explicit.  Keep one implementation so the
# capture and error rules cannot drift between server paths.
run_legacy_model_step = run_model_step


__all__ = ["run_model_step", "run_legacy_model_step", "wrap_model_generator"]
