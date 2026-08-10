"""SPEC-5 WP5: Tool service and execution contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.tools.descriptors import (
    ExecutionClass,
    ToolCall,
    ToolDescriptor,
    ToolEffect,
    ToolResult,
)
from sonder_runtime.domain.tools.policy import (
    GuardedToolPolicy,
    UnrestrictedToolPolicy,
)
from sonder_runtime.application.execution.tool_service import ToolService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    Forbidden,
    InvalidInput,
)
import time


# ---------------------------------------------------------------------------
# Domain: ToolDescriptor
# ---------------------------------------------------------------------------

class TestToolDescriptor:
    def test_frozen(self):
        d = ToolDescriptor(name="read_file")
        with pytest.raises(AttributeError):
            d.name = "other"  # type: ignore[misc]

    def test_effects_frozenset(self):
        d = ToolDescriptor(
            name="write_file",
            effects=frozenset({ToolEffect.WRITE_FILES}),
        )
        assert ToolEffect.WRITE_FILES in d.effects

    def test_default_execution_class(self):
        d = ToolDescriptor(name="pure_tool")
        assert d.execution_class == ExecutionClass.PURE


class TestToolCall:
    def test_frozen(self):
        c = ToolCall(tool_name="test")
        with pytest.raises(AttributeError):
            c.tool_name = "other"  # type: ignore[misc]


class TestToolResult:
    def test_default_success(self):
        r = ToolResult(tool_name="test", output="ok")
        assert r.success is True


# ---------------------------------------------------------------------------
# Domain: ToolPolicy — Guarded
# ---------------------------------------------------------------------------

class TestGuardedToolPolicy:
    def test_read_files_allowed(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="read_file",
            effects=frozenset({ToolEffect.READ_FILES}),
        )
        policy.authorize(desc, ToolCall(tool_name="read_file"))

    def test_execute_allowed(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            effects=frozenset({ToolEffect.EXECUTE}),
        )
        policy.authorize(desc, ToolCall(tool_name="run_code"))

    def test_write_files_denied_by_default(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="write_file",
            effects=frozenset({ToolEffect.WRITE_FILES}),
        )
        with pytest.raises(Forbidden):
            policy.authorize(desc, ToolCall(tool_name="write_file"))

    def test_network_denied_by_default(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="fetch_url",
            effects=frozenset({ToolEffect.NETWORK}),
        )
        with pytest.raises(Forbidden):
            policy.authorize(desc, ToolCall(tool_name="fetch_url"))

    def test_host_execution_downgraded_to_container(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            execution_class=ExecutionClass.HOST,
        )
        assert policy.select_execution_class(desc) == ExecutionClass.CONTAINER

    def test_container_preserved(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            execution_class=ExecutionClass.CONTAINER,
        )
        assert policy.select_execution_class(desc) == ExecutionClass.CONTAINER

    def test_pure_preserved(self):
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(name="search", execution_class=ExecutionClass.PURE)
        assert policy.select_execution_class(desc) == ExecutionClass.PURE

    def test_unsupported_container_fails_closed(self):
        """When no container is available, guarded policy still selects
        CONTAINER — the executor adapter is responsible for failing closed."""
        policy = GuardedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            execution_class=ExecutionClass.HOST,
        )
        assert policy.select_execution_class(desc) == ExecutionClass.CONTAINER


# ---------------------------------------------------------------------------
# Domain: ToolPolicy — Unrestricted
# ---------------------------------------------------------------------------

class TestUnrestrictedToolPolicy:
    def test_all_effects_allowed(self):
        policy = UnrestrictedToolPolicy()
        desc = ToolDescriptor(
            name="dangerous",
            effects=frozenset({
                ToolEffect.WRITE_FILES,
                ToolEffect.DELETE_FILES,
                ToolEffect.NETWORK,
                ToolEffect.GIT_WRITE,
                ToolEffect.PACKAGE_INSTALL,
                ToolEffect.SELFMOD,
            }),
        )
        policy.authorize(desc, ToolCall(tool_name="dangerous"))

    def test_host_executor_selected(self):
        policy = UnrestrictedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            execution_class=ExecutionClass.CONTAINER,
        )
        assert policy.select_execution_class(desc) == ExecutionClass.HOST

    def test_host_stays_host(self):
        policy = UnrestrictedToolPolicy()
        desc = ToolDescriptor(
            name="run_code",
            execution_class=ExecutionClass.HOST,
        )
        assert policy.select_execution_class(desc) == ExecutionClass.HOST

    def test_pure_stays_pure(self):
        policy = UnrestrictedToolPolicy()
        desc = ToolDescriptor(name="search", execution_class=ExecutionClass.PURE)
        assert policy.select_execution_class(desc) == ExecutionClass.PURE


# ---------------------------------------------------------------------------
# Application: ToolService
# ---------------------------------------------------------------------------

class _FakeRegistry:
    def __init__(self, tools=None):
        self._tools = {t.name: t for t in (tools or [])}

    def get(self, name):
        return self._tools.get(name)

    def list_all(self):
        return list(self._tools.values())


class _FakeExecutor:
    def __init__(self, result=None):
        self._result = result or ToolResult(tool_name="test", output="ok")
        self.calls = []

    def execute(self, descriptor, call, context, execution_class):
        self.calls.append((descriptor, call, context, execution_class))
        return self._result


def _wait_past(deadline_monotonic, limit_seconds=2.0):
    """Block until ``time.monotonic()`` is genuinely past ``deadline``.

    Sleeping a fixed amount does not work here. ``time.monotonic()`` has a
    15.625 ms resolution on Windows, so a ``sleep(0.01)`` used to verify a
    1 ms deadline advanced the clock by exactly zero in 14 of 40 measured
    runs -- both reads landed inside one tick -- and the test failed roughly
    one run in eight. Waiting on the clock the service actually reads makes
    it deterministic on any resolution.
    """
    if deadline_monotonic is None:
        raise AssertionError("context carries no deadline to wait past")
    give_up = time.monotonic() + limit_seconds
    while time.monotonic() <= deadline_monotonic:
        if time.monotonic() > give_up:
            raise AssertionError("clock never advanced past the deadline")
        time.sleep(0.005)


def _context(deadline=None, **overrides):
    kwargs = dict(
        correlation_id="test",
        source="repl",
        timeout_seconds=deadline,
    )
    kwargs.update(overrides)
    return local_owner_context(**kwargs)


class TestToolService:
    def test_execute_dispatches_through_pipeline(self):
        desc = ToolDescriptor(name="read_file", effects=frozenset({ToolEffect.READ_FILES}))
        registry = _FakeRegistry([desc])
        executor = _FakeExecutor()
        svc = ToolService(registry, GuardedToolPolicy(), executor)

        call = ToolCall(tool_name="read_file", arguments={"path": "/tmp/x"})
        result = svc.execute(call, _context())

        assert result.success is True
        assert result.tool_name == "read_file"
        assert len(executor.calls) == 1

    def test_unknown_tool_rejected(self):
        svc = ToolService(_FakeRegistry(), GuardedToolPolicy(), _FakeExecutor())
        with pytest.raises(InvalidInput, match="unknown tool"):
            svc.execute(ToolCall(tool_name="nope"), _context())

    def test_forbidden_effect_rejected(self):
        desc = ToolDescriptor(
            name="write_file",
            effects=frozenset({ToolEffect.WRITE_FILES}),
        )
        svc = ToolService(
            _FakeRegistry([desc]),
            GuardedToolPolicy(),
            _FakeExecutor(),
        )
        with pytest.raises(Forbidden):
            svc.execute(ToolCall(tool_name="write_file"), _context())

    def test_deadline_checked_before_call(self):
        desc = ToolDescriptor(name="test")
        svc = ToolService(
            _FakeRegistry([desc]),
            GuardedToolPolicy(),
            _FakeExecutor(),
        )
        ctx = _context(deadline=0.001)
        _wait_past(ctx.deadline_monotonic)
        with pytest.raises(DeadlineExceeded):
            svc.execute(ToolCall(tool_name="test"), ctx)

    def test_list_tools(self):
        descs = [
            ToolDescriptor(name="a"),
            ToolDescriptor(name="b"),
        ]
        svc = ToolService(
            _FakeRegistry(descs),
            GuardedToolPolicy(),
            _FakeExecutor(),
        )
        assert len(svc.list_tools()) == 2

    def test_unrestricted_uses_host_executor(self):
        desc = ToolDescriptor(
            name="run_code",
            effects=frozenset({ToolEffect.EXECUTE}),
            execution_class=ExecutionClass.CONTAINER,
        )
        executor = _FakeExecutor()
        svc = ToolService(
            _FakeRegistry([desc]),
            UnrestrictedToolPolicy(),
            executor,
        )
        svc.execute(ToolCall(tool_name="run_code"), _context())
        assert executor.calls[0][3] == ExecutionClass.HOST

    def test_output_bounded(self):
        """ToolResult captures output; bounded by the executor adapter."""
        long_output = "x" * 10000
        desc = ToolDescriptor(name="test")
        executor = _FakeExecutor(
            ToolResult(tool_name="test", output=long_output),
        )
        svc = ToolService(
            _FakeRegistry([desc]),
            GuardedToolPolicy(),
            executor,
        )
        result = svc.execute(ToolCall(tool_name="test"), _context())
        assert result.output == long_output
