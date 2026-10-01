"""File chat must use the existing guarded lanes, never a cwd write fallback."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.chat_file_routing import (
    INSPECTION_TOOLS, route_file_request,
)


def _host(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    calls = []
    host = SimpleNamespace(
        unsafe_lab=SimpleNamespace(active=lambda: False),
        served_work_project=lambda value: str(project) if value == str(project) else "",
        file_ops=SimpleNamespace(workspace_root=lambda: project),
        _agent_permission_gate_error=lambda *_a, **_k: "",
        directory_create=lambda path, **_k: Path(path).mkdir(parents=True) or "created",
        _agent_impl=lambda prompt, **kw: calls.append((prompt, kw)) or "3 Python files; largest: big.py (42 bytes)",
        _workbench_agent_escalating=lambda prompt, tier, **kw: calls.append((prompt, kw)) or ("written", tier),
    )
    return host, project, calls


def test_inspection_binds_read_only_allowlist_and_returns_tool_answer(tmp_path):
    host, project, calls = _host(tmp_path)
    result = route_file_request(host, "how many python files are in sonder_runtime?", "inspection", str(project), "code")
    assert "3 Python files" in result[0]
    assert calls[0][1]["read_only"] is True
    assert calls[0][1]["tool_allowlist"] == INSPECTION_TOOLS
    assert calls[0][1]["require_file_evidence"] is True
    assert calls[0][1]["project"] == str(project)
    assert calls[0][1]["allow_web"] is False
    assert "file_write" not in INSPECTION_TOOLS


def test_default_write_uses_unique_creation_folder(tmp_path):
    host, project, calls = _host(tmp_path)
    home = tmp_path / "state"

    def write(prompt, tier, **kw):
        destination = Path(kw["project"]) / "primes.py"
        destination.write_text("print(2)", encoding="utf-8")
        calls.append(destination)
        return "Saved primes.py", tier

    host._workbench_agent_escalating = write
    result = route_file_request(host, "save primes.py", "workbench", "default", "code", state_home=home)
    assert "Saved primes.py" in result[0]
    assert calls[0].is_relative_to(home / "creations")
    assert calls[0].parent.name.startswith("chat-")
    assert not (project / "primes.py").exists()


def test_refused_write_creates_no_directory_or_file(tmp_path):
    host, _, calls = _host(tmp_path)
    host._agent_permission_gate_error = lambda *_a, **_k: "Refused by plan mode"
    home = tmp_path / "state"
    result = route_file_request(host, "save primes.py", "workbench", "default", "code", state_home=home)
    assert "Refused by plan mode" in result[0]
    assert not home.exists()
    assert calls == []


@pytest.mark.parametrize("project", ["unknown-name", "../outside", "/outside"])
def test_unresolved_project_never_falls_back_to_cwd(tmp_path, project):
    host, _, calls = _host(tmp_path)
    result = route_file_request(host, "save primes.py", "workbench", project, "code", state_home=tmp_path / "state")
    assert "Refused" in result[0]
    assert calls == []


def test_unsafe_lab_cannot_remove_inspection_or_file_scope(tmp_path):
    host, project, calls = _host(tmp_path)
    host.unsafe_lab.active = lambda: True
    result = route_file_request(host, "read x.py", "inspection", str(project), "code")
    assert "Refused" in result[0]
    assert calls == []


def test_pinned_write_does_not_escalate_to_another_model(tmp_path):
    host, project, calls = _host(tmp_path)
    host.workbench_agent = lambda **kw: calls.append(kw) or "Saved primes.py"
    output, tier = route_file_request(
        host, "save primes.py", "workbench", str(project), "selected-model", pinned=True,
    )
    assert tier == "selected-model"
    assert "Saved primes.py" in output
    assert len(calls) == 1 and calls[0]["tier"] == "selected-model"
