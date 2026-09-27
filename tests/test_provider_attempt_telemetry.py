"""The dispatch_provider observer is content-free and can never fail a send."""
import pytest

from sonder_runtime.application.session import provider_attempts as attempts
from sonder_runtime.domain.common.errors import DependencyUnavailable


class RecordingObserver:
    def __init__(self):
        self.started = []
        self.finished = []
        self.fallbacks = []

    def provider_send_started(self, provider, operation, model):
        self.started.append((provider, operation, model))
        return len(self.started)

    def provider_send_finished(self, handle, **evidence):
        self.finished.append((handle, evidence))

    def provider_fallback(self, from_provider, to_provider, reason_code):
        self.fallbacks.append((from_provider, to_provider, reason_code))


@pytest.fixture
def observer():
    recorder = RecordingObserver()
    attempts.install_provider_attempt_observer(recorder)
    try:
        yield recorder
    finally:
        attempts.clear_provider_attempt_observer(recorder)


def test_observer_sees_model_and_usage_but_never_the_payload(observer):
    payload = {"model": "qwen", "messages": [{"role": "user", "content": "SECRET PROMPT"}]}
    reply = {"model": "qwen", "message": {"content": "SECRET ANSWER"},
             "prompt_eval_count": 12, "eval_count": 4}
    assert attempts.dispatch_provider("ollama", "/api/chat", payload, lambda: reply) is reply
    assert observer.started == [("ollama", "/api/chat", "qwen")]
    assert observer.finished == [(1, {"model": "qwen", "prompt_tokens": 12,
                                      "completion_tokens": 4})]
    assert "SECRET" not in repr(observer.started) + repr(observer.finished)


def test_openai_usage_shape_is_understood(observer):
    reply = {"model": "served", "choices": [{"message": {"content": "x"}}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
    attempts.dispatch_provider("openai-compatible", "/v1/chat/completions",
                               {"model": "default"}, lambda: reply)
    assert observer.finished[0][1] == {"model": "served", "prompt_tokens": 7,
                                       "completion_tokens": 2}


def test_failed_send_reports_the_domain_code_and_still_raises(observer):
    def send():
        raise DependencyUnavailable("refused")

    with pytest.raises(DependencyUnavailable):
        attempts.dispatch_provider("openai-compatible", "/v1/chat/completions",
                                   {"model": "m"}, send)
    assert observer.finished == [(1, {"error_code": "DEPENDENCY_UNAVAILABLE"})]


def test_a_broken_observer_never_fails_or_repeats_the_send():
    class Broken:
        def provider_send_started(self, *args):
            raise RuntimeError("observer bug")

        def provider_send_finished(self, *args, **kwargs):
            raise RuntimeError("observer bug")

    broken = Broken()
    sends = []
    attempts.install_provider_attempt_observer(broken)
    try:
        result = attempts.dispatch_provider("ollama", "/api/chat", {"model": "m"},
                                            lambda: sends.append(1) or {"ok": True})
    finally:
        attempts.clear_provider_attempt_observer(broken)
    assert result == {"ok": True}
    assert sends == [1]


def test_observer_runs_inside_a_capture_scope_too(observer):
    class Capture:
        def __init__(self):
            self.events = []

        def begin_provider_attempt(self, pending, **kwargs):
            self.events.append("begin")
            return "attempt-1"

        def finish_provider_attempt(self, pending, attempt, **kwargs):
            self.events.append("finish")

    capture = Capture()
    pending = type("Pending", (), {"session_id": "s"})()
    with attempts.provider_attempt_scope(capture, pending):
        attempts.dispatch_provider("ollama", "/api/chat", {"model": "m"},
                                   lambda: {"eval_count": 1})
    assert capture.events == ["begin", "finish"]
    assert observer.finished == [(1, {"model": None, "prompt_tokens": None,
                                      "completion_tokens": 1})]


def test_clearing_a_stale_observer_keeps_the_current_one(observer):
    attempts.clear_provider_attempt_observer(object())
    attempts.dispatch_provider("ollama", "/api/chat", {"model": "m"}, lambda: {})
    assert len(observer.started) == 1


def test_observer_must_implement_both_hooks():
    with pytest.raises(TypeError):
        attempts.install_provider_attempt_observer(object())


def test_fallback_reports_reach_observers_that_opt_in(observer):
    attempts.report_provider_fallback("sonder-inference", "ollama", "DEPENDENCY_UNAVAILABLE")
    assert observer.fallbacks == [("sonder-inference", "ollama", "DEPENDENCY_UNAVAILABLE")]


def test_fallback_report_without_observer_is_a_no_op():
    attempts.clear_provider_attempt_observer()
    attempts.report_provider_fallback("a", "b", "c")


class _KindedFailure(Exception):
    """Duck-typed like the legacy ModelCallError (kind + status)."""

    def __init__(self, kind, status=None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


class _HttpStatusFailure(OSError):
    """Duck-typed like urllib's HTTPError (an OSError with an int ``code``)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _WrappedTimeout(OSError):
    """Duck-typed like URLError(reason=timeout)."""

    def __init__(self):
        super().__init__("timed out")
        self.reason = TimeoutError("timed out")


@pytest.mark.parametrize("error,code", [
    (DependencyUnavailable("down"), "DEPENDENCY_UNAVAILABLE"),
    (ConnectionRefusedError(111, "Connection refused"), "DEPENDENCY_UNAVAILABLE"),
    (OSError("Connection refused"), "DEPENDENCY_UNAVAILABLE"),
    (TimeoutError("timed out"), "DEADLINE_EXCEEDED"),
    (_WrappedTimeout(), "DEADLINE_EXCEEDED"),
    (_HttpStatusFailure(503), "DEPENDENCY_UNAVAILABLE"),
    (_HttpStatusFailure(429), "CAPACITY_EXCEEDED"),
    (_HttpStatusFailure(400), "INVALID_INPUT"),
    (_KindedFailure("timeout"), "DEADLINE_EXCEEDED"),
    (_KindedFailure("cancelled"), "CANCELLED"),
    (_KindedFailure("provider_unavailable", 503), "DEPENDENCY_UNAVAILABLE"),
    (_KindedFailure("request", 429), "CAPACITY_EXCEEDED"),
    (KeyboardInterrupt(), "CANCELLED"),
    (ValueError("bad json"), "INTERNAL_FAILURE"),
])
def test_transport_failures_are_classified_as_domain_codes(error, code):
    assert attempts.telemetry_error_code(error) == code


def test_route_error_codes_are_never_python_class_names(observer):
    def send():
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(ConnectionRefusedError):
        attempts.dispatch_provider("ollama", "/api/chat", {"model": "q"}, send)
    assert observer.finished == [(1, {"error_code": "DEPENDENCY_UNAVAILABLE"})]
