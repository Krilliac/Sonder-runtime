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

# One process-wide, content-free observer of provider sends (installed by the
# composition root for live telemetry).  It sees the provider label, the
# operation, the model name and usage counts -- never a payload or response
# body -- and it can neither fail nor delay a send: every call into it is
# guarded, and it runs on the sending thread only for bookkeeping.
_attempt_observer = None


def install_provider_attempt_observer(observer) -> None:
    """Install the observer notified around every ``dispatch_provider`` send."""
    global _attempt_observer
    for name in ("provider_send_started", "provider_send_finished"):
        if not callable(getattr(observer, name, None)):
            raise TypeError("provider attempt observer must implement %s" % name)
    _attempt_observer = observer


def clear_provider_attempt_observer(observer=None) -> None:
    """Remove ``observer`` (or any observer when None); a stale owner is a no-op."""
    global _attempt_observer
    if observer is None or _attempt_observer is observer:
        _attempt_observer = None


def report_provider_fallback(from_provider, to_provider, reason_code) -> None:
    """Tell the observer a request moved to another provider before any send.

    A pre-send refusal (for example a cached not-ready health state) never
    reaches ``dispatch_provider``, so a fallback wrapper reports the change
    here.  Observers without ``provider_fallback`` ignore it; it never raises.
    """
    observer = _attempt_observer
    hook = getattr(observer, "provider_fallback", None)
    if not callable(hook):
        return
    try:
        hook(str(from_provider), str(to_provider), str(reason_code))
    except Exception:
        pass


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _response_evidence(result):
    """Extract (model, prompt_tokens, completion_tokens) from a provider reply.

    Both wire shapes are understood: Ollama's ``prompt_eval_count`` /
    ``eval_count`` and OpenAI-compatible ``usage``.  Text fields are never
    read.
    """
    if not isinstance(result, dict):
        return None, None, None
    model = result.get("model") if isinstance(result.get("model"), str) else None
    prompt, completion = _count(result.get("prompt_eval_count")), _count(result.get("eval_count"))
    usage = result.get("usage")
    if isinstance(usage, dict):
        if prompt is None:
            prompt = _count(usage.get("prompt_tokens"))
        if completion is None:
            completion = _count(usage.get("completion_tokens"))
    return model, prompt, completion


def _observed(provider, operation, payload, send):
    observer = _attempt_observer
    if observer is None:
        return send()
    requested = payload.get("model") if isinstance(payload, dict) else None
    try:
        handle = observer.provider_send_started(
            provider, operation, requested if isinstance(requested, str) else None,
        )
    except Exception:
        return send()
    try:
        result = send()
    except BaseException as error:
        code = error.code if isinstance(error, SonderError) else type(error).__name__
        try:
            observer.provider_send_finished(handle, error_code=str(code))
        except Exception:
            pass
        raise
    try:
        model, prompt_tokens, completion_tokens = _response_evidence(result)
        observer.provider_send_finished(
            handle, model=model, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    except Exception:
        pass
    return result


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
        return _observed(provider, operation, payload, send)
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
        result = _observed(provider, operation, payload, send)
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
