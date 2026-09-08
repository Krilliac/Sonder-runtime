"""Context-local ownership for final provider attempts; never replay authority.

Scopes bind in the execution thread. A returned JSON response is transport
evidence, not an accepted answer; validation/repair and transcript ownership
remain with the caller. Missing terminal evidence means an unknown outcome.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ...domain.common.errors import IntegrityFailure, InternalFailure, SonderError


_owner = ContextVar("provider_attempt_owner", default=None)


class ProviderCaptureFailure(IntegrityFailure):
    """Evidence storage failed; this is not a model transport failure."""


@dataclass
class _Owner:
    capture: object
    pending: object
    failure: ProviderCaptureFailure | None = None
    admit: object = None
    completed: object = None

    @property
    def admission(self):
        return (self.capture, self.pending) if self.pending is not None else None

    def fail(self, message, cause):
        self.failure = ProviderCaptureFailure(message)
        raise self.failure from cause


@contextmanager
def provider_attempt_scope(capture, pending):
    """Bind an admitted logical request, or explicitly disable capture with None."""
    owner = _Owner(capture, pending) if capture is not None and pending is not None else None
    token = _owner.set(owner)
    try:
        yield owner
        # Legacy repair/fallback callbacks may swallow Exception. A damaged
        # evidence scope must neither run another attempt nor publish success.
        if owner is not None and owner.failure is not None:
            raise owner.failure
    finally:
        _owner.reset(token)


@contextmanager
def deferred_provider_request_scope(admit):
    """Bind an explicit owner's admission callback; cache hits do not invoke it.

    Yield a scope whose ``admission`` is None before dispatch, or the capture
    service and committed request afterwards for surface-owned completion.
    None preserves an existing enclosing owner (including explicit opt-outs).
    """
    if admit is None or _owner.get() is not None:
        yield _owner.get()
        return
    owner = _Owner(None, None, admit=admit)
    token = _owner.set(owner)
    try:
        yield owner
        if owner.failure is not None:
            raise owner.failure
    finally:
        _owner.reset(token)


def complete_scoped_provider_request(session_id, response):
    """Complete an admitted legacy turn once; None retains retrospective capture."""
    owner = _owner.get()
    if owner is None or owner.pending is None or owner.pending.session_id != str(session_id):
        return None
    if owner.failure is not None:
        raise owner.failure
    if owner.completed is None:
        try:
            owner.completed = owner.capture.complete_request(owner.pending, model_response=response)
        except Exception as error:
            owner.fail("could not persist logical response", error)
    return owner.completed


def dispatch_provider(provider, operation, payload, send):
    """Commit the effective JSON body before exactly one transport invocation.

    No headers or endpoint URL enter this API. Capture errors are intentionally
    outside transport exception types so existing retry policies cannot replay
    a request after an evidence write failed. BaseException preserves an
    unresolved admission instead of manufacturing a known provider failure.
    """
    owner = _owner.get()
    if owner is None:
        return send()
    if owner.failure is not None:
        raise owner.failure
    if owner.pending is None:
        try:
            owner.capture, owner.pending = owner.admit()
        except Exception as error:
            owner.fail("could not persist logical admission", error)
    capture, pending = owner.capture, owner.pending
    try:
        attempt = capture.begin_provider_attempt(pending, provider=provider, operation=operation, payload=payload)
    except Exception as error:
        owner.fail("could not persist provider admission", error)
    try:
        result = send()
    except Exception as error:
        code = error.code if isinstance(error, SonderError) else InternalFailure.code
        try:
            capture.finish_provider_attempt(pending, attempt, error_code=code)
        except Exception as capture_error:
            owner.fail("could not persist provider failure", capture_error)
        raise
    try:
        capture.finish_provider_attempt(pending, attempt, response=result)
    except Exception as error:
        owner.fail("could not persist provider response", error)
    return result
