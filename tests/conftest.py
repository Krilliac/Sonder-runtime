"""Pytest-wide isolation for process-shared Sonder runtime state."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_FLEET_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sonder-pytest-fleet-"))

# This hook module is loaded before pytest imports test modules.  Pinning the
# environment here ensures fleet_store/master_orchestrator never open the live
# restart-safe ledger during collection, setup_function(), subprocess tests, or
# importlib.reload().  The old suite called reset_for_tests() against the live
# database and could cancel an operator's active fleet.
os.environ["SONDER_FLEET_DB"] = str(_FLEET_TEST_ROOT / "fleet.db")
os.environ["SONDER_FLEET_PRINCIPAL_FILE"] = str(
    _FLEET_TEST_ROOT / "fleet-principal.json"
)


def _cleanup_test_fleet_root():
    shutil.rmtree(_FLEET_TEST_ROOT, ignore_errors=True)


# Registered before test modules import master_orchestrator, so LIFO atexit
# ordering lets its owner finalizer close the isolated ledger before deletion.
# Do not restore SONDER_FLEET_DB inside this process: a later atexit callback or
# daemon thread must never fall back to the operator's live database.
atexit.register(_cleanup_test_fleet_root)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_fleet_ledger():
    """Clear the shared fleet ledger before each test.

    fleet_store is a process-shared sqlite ledger, and a test that leaves
    active agents behind pollutes any later test that reads fleet state. This
    surfaced when a test left an in-model-call agent and unrelated learning
    tests then saw active_model_call_count() > 0 and deferred distillation.
    Clearing before each test keeps the suite order-independent.
    """
    try:
        import sonder_runtime.adapters.persistence.fleet_store as fleet_store
        fleet_store.clear_all()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_typed_ollama_endpoint():
    """Restore the process-global typed Ollama endpoint around each test.

    ``bootstrap.app`` pins the typed endpoint (``configure_typed_endpoint``)
    when it composes an application from a config, and from then on the
    process ignores ``OLLAMA_HOST``. A composition test that never called
    ``reset_for_tests`` therefore made every later origin-policy test in the
    same process silently test the pinned default instead of its monkeypatched
    host -- ``test_promotion_eval``'s loopback rejections failed only when
    scheduled after a composing test on the same xdist worker. Save/restore
    rather than unconditional reset, so a test that legitimately configures
    the endpoint still sees its own value while it runs.
    """
    from sonder_runtime.adapters.inference import ollama_endpoint

    with ollama_endpoint._configuration_lock:
        before = ollama_endpoint._configured_endpoint
    yield
    ollama_endpoint.configure_typed_endpoint(before)


@pytest.fixture(autouse=True)
def _configure_http_legacy_boundary(monkeypatch):
    """Exercise the same explicit runtime injection as the serve bootstrap."""
    import server
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    # Rebind through monkeypatch so tests that exercise reloads or substitute
    # a small runtime double cannot leak that process-global composition state
    # into the next test (especially under xdist workers).
    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", server)
    monkeypatch.setattr(repl, "_legacy_runtime", server)
    from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway

    OllamaGateway.configure_default_providers(
        target_resolver=lambda tier, strict=False: _legacy_model_target(
            server, tier, strict
        ),
        generate_factory=lambda model, system, temperature, num_predict, num_ctx, **kwargs: server._make_generate(
            model, system, temperature, num_predict, num_ctx, **kwargs
        ),
    )
    yield


def _legacy_model_target(server, tier, strict):
    from sonder_runtime.application.ports.model_target import ModelTarget

    model, cloud, augment, tier_label = server._serve_target(tier, strict)
    return ModelTarget(model, cloud, tier_label, augment)


@pytest.fixture
def without_standing():
    """Drop the calibration standing an agent end report may now carry.

    ``_agent_impl`` prefixes a measured standing when the caller-judged record
    demands verification and the run cited none. The hermetic test store is
    empty, so under pytest the record is always ``unmeasured`` -- which fails
    closed by design, and would otherwise rewrite the expected output of every
    unrelated agent-loop test.

    Use this only in tests that are about something else (tool dispatch,
    caching, evidence attachment). It deliberately does not assert the standing
    is present -- ``tests/test_agent_verification_gate.py`` owns that -- so a
    test using it keeps checking exactly the text it checked before.
    """
    def _strip(text):
        import server

        text = str(text or "")
        if text.startswith(server._AGENT_UNVERIFIED_PREFIX):
            return text.split("\n\n", 1)[1]
        return text

    return _strip
