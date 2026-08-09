"""Server integration for bounded static artifact risk inspection."""
from __future__ import annotations

import json

import artifact_risk
import file_ops
import server


def _root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    return root


def test_direct_inspection_reports_without_returning_content(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    marker = "DO_NOT_RETURN_THIS_PAYLOAD"
    path = root / "payload.ps1"
    path.write_text("powershell -EncodedCommand AAAA\n" + marker, encoding="utf-8")

    output = server.artifact_risk_inspect(str(path))
    result = json.loads(output)

    assert result["risk"] == "high"
    assert result["execution"] == "none"
    assert marker not in output


def test_project_agent_rebases_and_dispatches_read_only(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    (root / "sample.bin").write_bytes(b"opaque")

    output = server._agent_dispatch(
        "artifact_risk_inspect", {"path": "sample.bin"},
        read_only=True, repository_extra_roots=str(root),
    )

    assert json.loads(output)["path"] == str(root / "sample.bin")
    assert "artifact_risk_inspect" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "artifact_risk_inspect" in server._WORK_INSPECTION_TOOLS
    assert "artifact_risk_inspect" in server._AGENT_FILE_EVIDENCE_TOOLS
    assert "artifact_risk_inspect" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS


def test_project_agent_rejects_outside_path(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"opaque")
    output = server._agent_dispatch(
        "artifact_risk_inspect", {"path": str(outside)},
        read_only=True, repository_extra_roots=str(root),
    )
    assert output.startswith("ERROR:")


def test_script_run_deny_high_prevents_execution(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    path = root / "payload.ps1"
    path.write_text("powershell -EncodedCommand AAAA", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "deny-high")
    calls = []
    monkeypatch.setattr(server.workbench, "run_script", lambda *a, **k: calls.append((a, k)))

    output = server.script_run(str(path), risk_policy="off")

    assert output.startswith("artifact risk: {")
    assert '"denied":true' in output
    assert "execution denied by effective policy deny-high" in output
    assert calls == []


def test_enforcing_policy_refuses_below_threshold_path_handoff(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    path = root / "safe.py"
    path.write_text("print('safe')", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "deny-high")
    calls = []
    monkeypatch.setattr(server.workbench, "run_script", lambda *a, **k: calls.append((a, k)))
    output = server.script_run(str(path))
    assert "exact_execution_handoff_unavailable" in output
    assert calls == []


def test_script_run_report_includes_risk_before_execution(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    path = root / "safe.py"
    path.write_text("print('ok')", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "report")
    monkeypatch.setattr(
        server.workbench, "run_script",
        lambda *a, **k: {"ok": True, "returncode": 0, "command": ["python", str(path)],
                         "stdout": "ok\n", "stderr": "", "timed_out": False,
                         "truncated": False, "duration_ms": 1},
    )

    output = server.script_run(str(path), risk_policy="off")

    assert output.startswith("artifact risk: {")
    assert '"policy":"report"' in output
    assert "script run" in output


def test_manifest_help_autopilot_and_reload_contract():
    assert "artifact_risk" in server.LIVE_RELOAD_MODULES
    assert "artifact_risk_inspect" in server.tool_manifest()
    assert "artifact_risk_inspect" in server.AGENT_TOOL_HELP
    assert "artifact_risk_inspect" in server.REPOSITORY_AGENT_TOOL_HELP
    assert "artifact_risk_inspect" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "artifact_risk_inspect" in server._AUTOPILOT_OBSERVE_TOOLS
    assert server._autopilot_tool_policy({"policy": "observe"})(
        "artifact_risk_inspect", {"path": "artifact.bin"},
    ) == ""


def test_operator_policy_precedence_is_shared_with_server(monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "deny-medium")
    assert artifact_risk.effective_policy("report") == "deny-medium"
