import pytest

from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle


def test_owned_application_is_exact_and_terminal_without_factory_fallback():
    calls = []
    lifecycle = ApplicationLifecycle(lambda: calls.append("constructed"))
    application = object()
    lifecycle.install_owned(application)
    assert lifecycle.get() is application
    with pytest.raises(RuntimeError):
        lifecycle.reset()
    lifecycle.stop_owned(application)
    with pytest.raises(RuntimeError):
        lifecycle.get()
    with pytest.raises(RuntimeError):
        lifecycle.install_owned(object())
    assert calls == []


def test_owned_installation_cannot_adopt_used_or_failed_factory():
    lifecycle = ApplicationLifecycle(object)
    lifecycle.get()
    lifecycle.reset()
    with pytest.raises(RuntimeError):
        lifecycle.install_owned(object())
    def failed():
        raise RuntimeError("constructor failed")
    lifecycle = ApplicationLifecycle(failed)
    with pytest.raises(RuntimeError):
        lifecycle.get()
    with pytest.raises(RuntimeError):
        lifecycle.install_owned(object())
