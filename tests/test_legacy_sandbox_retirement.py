"""The retired compatibility API cannot silently execute on the host."""
import subprocess
import tempfile

import pytest

from sonder_runtime.adapters.execution import sandbox
from sonder_runtime.domain.execution import sandbox as legacy_types


@pytest.mark.parametrize("level", list(sandbox.IsolationLevel))
@pytest.mark.parametrize("python_helper", [False, True])
def test_retired_runner_is_unavailable_before_any_host_effect(monkeypatch, level, python_helper):
    def forbidden(*args, **kwargs):
        pytest.fail("retired sandbox attempted a process or temporary file")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbidden)
    policy = sandbox.SandboxPolicy(level=level, allow_network=False,
                                   allowed_paths=("restricted",), max_memory_mb=1)
    result = (sandbox.run_python_isolated("print('unused')", policy=policy)
              if python_helper else sandbox.run_isolated(["unused"], policy=policy))
    assert not result.ok
    assert result.exit_code == -1
    assert result.error.startswith("LEGACY_SANDBOX_UNAVAILABLE:")
    assert result.stdout == result.stderr == ""
    assert not result.timed_out


def test_retired_adapter_uses_one_compatibility_type_family():
    assert sandbox.IsolationLevel is legacy_types.IsolationLevel
    assert sandbox.SandboxResult is legacy_types.SandboxResult
