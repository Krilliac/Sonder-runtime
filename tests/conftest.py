"""Pytest-wide isolation for process-shared Sonder runtime state."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_FLEET_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sonder-pytest-fleet-"))

# Tests that started with the state home outside every test-owned directory.
_ESCAPED_STATE_HOMES: list[tuple[str, str]] = []


def _test_owned(path: Path) -> bool:
    """Whether *path* is inside the system temp directory.

    The repository-root conftest creates the session's state home with
    ``mkdtemp`` and every ``tmp_path`` lives there too; an operator's real
    state home (``%LOCALAPPDATA%\\sonder``, ``~/.local/share/sonder``) never
    does. Deliberately not trusting ``SONDER_HOME`` itself: if the root conftest
    did not load, that variable may name the real home.
    """
    try:
        resolved = Path(path).resolve()
        root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True

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
def _isolate_runtime_home():
    """Restore process-local path configuration after entrypoint tests.

    Typed startup deliberately overrides per-store environment paths. Leaving
    that override behind makes later tests write into an earlier test's home
    even when they set their own database environment variables.
    """
    from sonder_runtime.platform import paths

    previous = paths._configured_home()
    try:
        yield
    finally:
        if previous is None:
            paths.reset_home()
        else:
            paths.configure_home(previous)



def emotion_vector_cleanup(live_copy: Path):
    """Return ``(owned, remove)`` for one test's emotion-vector copy.

    ``remove`` deletes *live_copy* only when it lies in a test-owned
    directory; for anything else it is a no-op, so no test run can delete an
    operator's live tuning file.
    """
    owned = _test_owned(live_copy)

    def remove():
        if not owned:
            return
        try:
            live_copy.unlink()
        except (FileNotFoundError, OSError):
            pass

    return owned, remove

@pytest.fixture(autouse=True)
def _isolate_emotion_vectors_state(request, _isolate_runtime_home):
    """Drop the live emotion-vector copy from the shared test state home.

    Live tuning writes ``<state home>/emotion_vectors.json``, which then
    shadows the bundled default for every later prompt build.  Remove it on
    both sides of each test so no test inherits another's tone vectors.

    Only ever inside a test-owned home. If an earlier test leaked a real state
    home (dropped SONDER_HOME, an override left behind), the path below is an
    operator's live tuning file: deleting it before and after every later test
    silently destroyed the user's vectors on any local run. The escape is
    recorded and reported at the end of the session instead.
    """
    from sonder_runtime.platform import paths

    # Resolve once, before the test runs: platform-simulation tests patch
    # ``os.name``/path flavours, and re-resolving the home under those patches
    # at teardown tried to build a WindowsPath on POSIX.
    live_copy = paths.default_home() / "emotion_vectors.json"
    owned, remove = emotion_vector_cleanup(live_copy)
    if not owned:
        _ESCAPED_STATE_HOMES.append((request.node.nodeid, str(live_copy.parent)))

    remove()
    yield
    remove()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Name every test that ran against a state home outside the test root."""
    del exitstatus, config
    if not _ESCAPED_STATE_HOMES:
        return
    terminalreporter.section("state home escaped test isolation", sep="=", red=True)
    for nodeid, home in _ESCAPED_STATE_HOMES[:20]:
        terminalreporter.write_line(f"{nodeid}: {home}")
    if len(_ESCAPED_STATE_HOMES) > 20:
        terminalreporter.write_line(f"... and {len(_ESCAPED_STATE_HOMES) - 20} more")


@pytest.fixture(autouse=True)
def _no_incidental_prewarm(request, monkeypatch):
    """Keep incidental HTTP chat tests from prewarming a real local model.

    Every served chat calls server.prewarm_model, which starts a thread that
    loads the tier's model from the configured Ollama -- on a developer machine
    the real one. Those threads outlived their tests, reached the live Ollama,
    and competed with later tests (an intermittent 5 s client timeout in
    test_serve_auth). Tests that exercise prewarm opt back in with
    @pytest.mark.real_prewarm.
    """
    if request.node.get_closest_marker("real_prewarm") is None:
        monkeypatch.setenv("SONDER_PREWARM", "0")


@pytest.fixture(autouse=True)
def _isolate_routing_environment(monkeypatch):
    """Restore deployment routing variables after every test.

    A few legacy tests intentionally exercise direct ``os.environ`` updates
    rather than ``monkeypatch``. Without a per-test snapshot, a remote worker
    or selected model can leak into a later xdist test process and change its
    configuration before that test gets a chance to set its own inputs.
    """
    del monkeypatch
    before = {
        name: value
        for name, value in os.environ.items()
        if name.upper().startswith(("SONDER_", "OLLAMA_"))
    }
    yield
    for name in tuple(os.environ):
        if name.upper().startswith(("SONDER_", "OLLAMA_")):
            if name not in before:
                os.environ.pop(name, None)
    os.environ.update(before)


@pytest.fixture(autouse=True)
def _isolate_fleet_ledger(_isolate_runtime_home):
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
def _isolate_legacy_server_graph():
    """Start each test without a graph ``server._application()`` built earlier.

    The legacy module composes its graph lazily and keeps it, owned, for the
    life of the process. So the first test on an xdist worker to reach it left
    every later test there running against a graph composed for someone else,
    with the module's ownership flag set -- and a later test that bound its
    own impostor graph saw ``run_mcp`` retire the impostor instead:
    ``'types.SimpleNamespace' object has no attribute 'close_providers'``.
    Retire exactly the graph the module constructed, whether it is still
    bound or a monkeypatch restoring ``_APP_GRAPH`` has already orphaned it;
    a graph another owner bound is not the module's to close. A double that
    a test's patched ``build_application`` returned is only dropped.
    """
    import server
    from sonder_runtime.bootstrap.application_graph import Application

    with server._APP_GRAPH_LOCK:
        built = server._APP_GRAPH_BUILT_BY_SERVER
        if built is not None and server._APP_GRAPH is built:
            server._APP_GRAPH = None
        server._APP_GRAPH_BUILT_BY_SERVER = None
        server._APP_GRAPH_OWNED_BY_SERVER = False
    if type(built) is Application:
        built.close_providers(timeout=5)
    yield


@pytest.fixture(autouse=True)
def _isolate_typed_ollama_endpoint(monkeypatch):
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
    from sonder_runtime.adapters import embeddings as embeddings
    import weakref

    # Each test owns its composed sources. Keep unrelated external-source
    # cycles from another test from restricting this test's static adapter.
    monkeypatch.setattr(ollama_endpoint, "_external_membership_owners", weakref.WeakSet())

    with ollama_endpoint._configuration_lock:
        before = ollama_endpoint._configured_endpoint
    before_base = embeddings.BASE
    before_netloc = embeddings.OLLAMA_HOST
    before_embedding = (
        embeddings.EMBED_MODEL,
        embeddings.EMBED_IDENTITY,
        embeddings.EMBED_REVISION,
        embeddings.EXPECTED_DIMENSION,
    )
    yield
    ollama_endpoint.configure_typed_endpoint(before)
    # Restore the frozen embeddings origin by assignment. Calling
    # configure_typed_endpoint(None) re-reads OLLAMA_HOST while monkeypatch
    # teardown has not run yet, so a test that set a malformed host
    # (e.g. http://[::1) blew up here with ValueError: Invalid IPv6 URL.
    embeddings.BASE = before_base
    embeddings.OLLAMA_HOST = before_netloc
    (
        embeddings.EMBED_MODEL,
        embeddings.EMBED_IDENTITY,
        embeddings.EMBED_REVISION,
        embeddings.EXPECTED_DIMENSION,
    ) = before_embedding


@pytest.fixture(autouse=True)
def _isolate_fleet_worker_cap():
    """Do not inherit a leftover configure_fleet_worker_cap across tests.

    Composition/capacity tests pin ``_FLEET_WORKER_CAP`` (often 2). When that
    value ties hardware slot limits, ``capacity()`` reports ``bound_by`` as
    ``fleet_workers`` instead of ``ram`` / ``gpu_vram`` / ``ollama_num_parallel``,
    which breaks the hardware capacity suite under xdist.
    """
    import master_orchestrator

    before = master_orchestrator._FLEET_WORKER_CAP
    master_orchestrator._FLEET_WORKER_CAP = None
    try:
        yield
    finally:
        master_orchestrator._FLEET_WORKER_CAP = before


@pytest.fixture(autouse=True)
def _configure_http_legacy_boundary(monkeypatch):
    """Exercise the same explicit runtime injection as the serve bootstrap.

    Live-provider credentials are restored only by tests that explicitly
    request ``live_provider_environment``; this suite-wide boundary must stay
    hermetic even when the live-test command-line flags are enabled.
    """
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
def isolated_default_runtime(monkeypatch):
    """Give one test an empty process-default application runtime.

    ``bootstrap.app`` keeps the default graph's cleanup callbacks beside the
    lifecycle that holds it. Tests that swapped in a fresh lifecycle but only
    some of those callbacks left the rest bound to a graph an earlier test had
    composed: their ``default_app(config=...)`` claimed that graph's cleanup,
    closed it, and reset only the swapped-in lifecycle, so once monkeypatch
    restored the original lifecycle every later ``default_app()`` on the
    worker got a closed graph ("configured membership must be active").
    Every ``_default_*_close`` callback is swapped, found by name rather than
    a hand-kept list that falls behind the next one added.
    """
    from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
    from sonder_runtime.bootstrap import app as bootstrap

    callbacks = [name for name in vars(bootstrap)
                 if name.startswith("_default_") and name.endswith("_close")]
    assert "_default_application_close" in callbacks, callbacks
    monkeypatch.setattr(bootstrap, "_application_lifecycle",
                        ApplicationLifecycle(bootstrap._build_default_application))
    for name in (*callbacks, "_default_config", "_owned_default_application"):
        monkeypatch.setattr(bootstrap, name, None)
    monkeypatch.setattr(bootstrap, "_default_runtime_closing", False)
    return bootstrap


@pytest.fixture
def unattended_effects_allowed():
    """``auto`` for one test that drives an unattended surface through an effect.

    With nobody to ask, ``permission_modes`` refuses file changes and host
    programs under the default ``manual`` instead of assuming the answer, so a
    test that exercises where a tool call goes -- dispatch, routing, argument
    plumbing -- and not whether the gate lets it needs the one mode that
    answers those classes for an unattended caller. Which decisions the gate
    makes is owned by ``tests/test_permission_*.py``; use this only in tests
    about something else. The previous mode is put back afterwards.
    """
    import permission_modes

    before = permission_modes.current_mode()
    permission_modes.set_mode(permission_modes.AUTO)
    try:
        yield
    finally:
        permission_modes.set_mode(before)


@pytest.fixture
def every_tool_allowed_by_rule(monkeypatch):
    """A written allow rule for every tool, for one test.

    An explicit allow satisfies the mode's ask in every mode, including for
    the ``dangerous`` class that no mode answers for an unattended caller. It
    also lifts the shipped ``file_delete`` deny, so a test that relies on that
    rule must not use this. As above: for tests about routing, never about
    the gate.
    """
    import permission_modes

    monkeypatch.setattr(
        permission_modes, "_rule_lookup",
        lambda tool: {"pattern": tool, "action": permission_modes.ALLOW, "note": "test"},
    )
    yield


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
