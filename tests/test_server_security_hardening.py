"""Regression tests for Wave 2 server.py security & reliability hardening.

Covers:
- schema_validate project-scope rebasing and schema_path confinement (#179 #1)
- _repository_scope_path_error UNC/network project_root rejection before resolve (#178 #2)
- artifact_ground and verify_artifact read-access guards and read-only policy (#179 #3)
- test_discover static-only discovery under read_only=True and plan mode (#179 #4)
- served_work_project exclusion of checkout/state home and relative paths (#179 #6)
- _scope_arg_is_rooted drive-relative detection & single-file empty-path scoping (#179 #9, #15)
- master_orchestrate/master/master_retry/agent_retry depth & project-root guards (#184 #1)
- HostObservationLedger protection against _TRUSTED_REPOSITORY_APPROVAL sentinel (#184 #4)
- Speculation guards for allowed_tools and tool_policy (#184 #14)
- post_status -> report_progress alias canonicalization (#184 #16)
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.application.agents import delegation_runtime
from sonder_runtime.domain.agents import tool_naming


# --- schema_validate confinement ---------------------------------------------


def test_schema_validate_project_scope_rebases_and_confines_schema_path(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    data_file = project / "config.json"
    data_file.write_text('{"ok": true}\n', encoding="utf-8")
    outside_schema = outside / "secret_schema.json"
    outside_schema.write_text('{"type": "object"}\n', encoding="utf-8")

    scoped = server._project_scope_args(
        "schema_validate",
        {"path": "config.json", "schema_path": "schemas/config.schema.json"},
        str(project),
    )
    assert Path(scoped["path"]) == project / "config.json"
    assert Path(scoped["schema_path"]) == project / "schemas" / "config.schema.json"

    # Empty schema_path stays empty (supports inline schema_json).
    scoped_inline = server._project_scope_args(
        "schema_validate",
        {"path": "config.json", "schema_json": '{"type": "object"}'},
        str(project),
    )
    assert not scoped_inline.get("schema_path")

    # Escaping schema_path is refused by _repository_scope_path_error.
    err_abs = server._repository_scope_path_error(
        "schema_validate",
        {"path": str(data_file), "schema_path": str(outside_schema)},
        str(project),
    )
    assert err_abs.startswith("ERROR:")
    assert "outside the host-selected project root" in err_abs

    err_rel = server._repository_scope_path_error(
        "schema_validate",
        {"path": "config.json", "schema_path": "../outside/secret_schema.json"},
        str(project),
    )
    assert err_rel.startswith("ERROR:")
    assert "outside the host-selected project root" in err_rel

    # Full observed dispatch refuses even on read_only=False (where _TRUSTED_REPOSITORY_APPROVAL is injected).
    dispatched = server._agent_dispatch_observed(
        "schema_validate",
        {"path": "config.json", "schema_path": str(outside_schema)},
        read_only=False,
        project=str(project),
    )
    assert dispatched.startswith("ERROR:")
    assert "outside the host-selected project root" in dispatched


# --- _repository_scope_path_error network project_root -----------------------


@pytest.mark.parametrize(
    "bad_root",
    [
        r"\\attacker\share\repo",
        "//attacker/share/repo",
        r"\??\C:\Windows",
        r"\\?\UNC\attacker\share",
    ],
)
def test_repository_scope_path_error_rejects_network_project_root_before_resolve(
    monkeypatch, bad_root,
):
    resolved_calls = []
    real_resolve = Path.resolve

    def spy_resolve(self, *args, **kwargs):
        resolved_calls.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    err = server._repository_scope_path_error("file_read", {"path": "a.txt"}, bad_root)
    assert err.startswith("ERROR:")
    assert "project root" in err
    assert resolved_calls == []


# --- artifact_ground & verify_artifact read-access guards --------------------


def test_artifact_ground_and_verify_artifact_block_sensitive_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=topsecret\n", encoding="utf-8")
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text('{"api_key": "topsecret"}\n', encoding="utf-8")

    ag_env = server.artifact_ground(path=str(env_file))
    assert ag_env.startswith("ERROR:")
    ag_sec = server.artifact_ground(path=str(secrets_file))
    assert ag_sec.startswith("ERROR:")

    va_env = server.verify_artifact(path=str(env_file))
    assert va_env.startswith("artifact verify REFUSED:")
    va_sec = server.verify_artifact(path=str(secrets_file))
    assert va_sec.startswith("artifact verify REFUSED:")

    # Read-only repository policy also rejects both tools on sensitive files.
    for tool_name in ("artifact_ground", "verify_artifact"):
        ro_err = server._repository_read_only_error(
            tool_name, {"path": str(env_file)}, trusted_extra_roots=str(tmp_path),
        )
        assert ro_err.startswith("ERROR:"), f"{tool_name} allowed .env in read-only"
        ro_err2 = server._repository_read_only_error(
            tool_name, {"path": str(secrets_file)}, trusted_extra_roots=str(tmp_path),
        )
        assert ro_err2.startswith("ERROR:"), f"{tool_name} allowed secrets.json in read-only"


# --- test_discover static-only under read_only=True and plan mode ------------


def test_test_discover_is_static_only_under_read_only_and_plan_mode(monkeypatch, tmp_path):
    import permission_modes as pm

    project = tmp_path / "proj"
    project.mkdir()
    sentinel = project / "pwned.txt"
    (project / "conftest.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n"
        f"raise RuntimeError('conftest must never execute')\n",
        encoding="utf-8",
    )
    (project / "test_sample.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n",
        encoding="utf-8",
    )

    spawned = []
    monkeypatch.setattr(
        server.harness_tools, "test_discover",
        lambda **kw: spawned.append(kw) or {"framework": "pytest", "test_count": 0, "test_files": []},
    )

    # read_only=True agent dispatch uses static AST scan, never executes conftest or spawns pytest.
    ro_out = server._agent_dispatch_observed(
        "test_discover", {"root": "."}, read_only=True, project=str(project),
    )
    assert "test discovery: pytest" in ro_out
    assert "tests: 2" in ro_out
    assert "test_sample.py" in ro_out
    assert spawned == []
    assert not sentinel.exists()
    assert not (project / "__pycache__").exists()
    assert not (project / ".pytest_cache").exists()

    # plan mode direct call also uses static AST scan.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    before = pm.current_mode()
    try:
        pm.set_mode(pm.PLAN)
        plan_out = server.test_discover(root=str(project))
        assert "test discovery: pytest" in plan_out
        assert "tests: 2" in plan_out
        assert spawned == []
        assert not sentinel.exists()
    finally:
        pm.set_mode(before)


# --- served_work_project confinement -----------------------------------------


def test_served_work_project_rejects_checkout_state_home_and_relative_paths(
    monkeypatch, tmp_path,
):
    allowed_dir = tmp_path / "client_proj"
    allowed_dir.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed_dir))

    assert server.served_work_project(str(allowed_dir)) == str(allowed_dir.resolve())
    # Rejects Sonder's own checkout and state home.
    assert server.served_work_project(str(file_ops.workspace_root())) == ""
    home = file_ops._validated_state_home()
    if home is not None:
        assert server.served_work_project(str(home)) == ""
    # Rejects relative and drive-relative spellings.
    assert server.served_work_project(".") == ""
    assert server.served_work_project("./client_proj") == ""
    assert server.served_work_project("D:client_proj") == ""


# --- _scope_arg_is_rooted & single-file empty-path scoping -------------------


def test_scope_arg_is_rooted_catches_drive_relative_and_keeps_empty_single_file_paths(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()

    assert server._scope_arg_is_rooted("D:foo") is True
    assert server._scope_arg_is_rooted("C:bar\\baz.py") is True
    assert server._scope_arg_is_rooted("sub/file.py") is False

    # Drive-relative is NOT joined under project and IS rejected by scope check.
    scoped_drive = server._project_scope_args("file_read", {"path": "D:foo"}, str(project))
    assert scoped_drive["path"] == "D:foo"
    scope_err = server._repository_scope_path_error("file_read", scoped_drive, str(project))
    assert scope_err.startswith("ERROR:")

    # Single-file tools do NOT default empty path to the project directory.
    for tool_name in ("file_read", "file_write", "file_delete", "file_edit", "json_patch"):
        scoped_empty = server._project_scope_args(tool_name, {"path": ""}, str(project))
        assert scoped_empty["path"] == "", f"{tool_name} rewrote empty path to {scoped_empty['path']!r}"

    # Directory-taking tools STILL default empty path/root to project.
    for tool_name, key in (
        ("directory_tree", "path"),
        ("workspace_inventory", "path"),
        ("code_metrics", "path"),
        ("file_find", "root"),
        ("text_search", "root"),
    ):
        scoped_dir = server._project_scope_args(tool_name, {}, str(project))
        assert scoped_dir[key] == str(project)


# --- master_orchestrate / master_retry depth & project-root guards -----------


def test_master_tools_enforce_delegation_depth_and_project_root_containment(
    monkeypatch, tmp_path, unattended_effects_allowed,
):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    orch_calls = []
    retry_calls = []
    monkeypatch.setattr(
        server, "master_orchestrate",
        lambda **kw: orch_calls.append(kw) or "ok-orch",
    )
    monkeypatch.setattr(
        server, "master_retry",
        lambda **kw: retry_calls.append(kw) or "ok-retry",
    )

    # At depth 1, both _agent_run_tool_refusal and _agent_dispatch refuse all 4 names.
    with delegation_runtime.depth_scope(1):
        for name in ("master_orchestrate", "master", "master_retry", "agent_retry"):
            assert server._agent_run_tool_refusal(name) == "delegation depth limit"
            out = server._agent_dispatch(name, {"task": "t", "agent_id": "a1"})
            assert out.startswith("ERROR:")
            assert "depth limit" in out
    assert orch_calls == []
    assert retry_calls == []

    # At depth 0 with repository_extra_roots set, outside project is rejected even if in SONDER_FILE_ROOTS.
    refused = server._agent_dispatch(
        "master_orchestrate",
        {"task": "t", "project": str(outside)},
        repository_extra_roots=str(project),
    )
    assert refused.startswith("ERROR:")
    assert "outside the host-selected project root" in refused
    assert orch_calls == []

    # Empty project defaults to repository_extra_roots; inside subdir succeeds.
    sub = project / "sub"
    sub.mkdir()
    ok_default = server._agent_dispatch(
        "master_orchestrate",
        {"task": "t"},
        repository_extra_roots=str(project),
    )
    assert ok_default == "ok-orch"
    assert orch_calls[-1]["project"] == str(project)

    ok_sub = server._agent_dispatch(
        "master_orchestrate",
        {"task": "t", "project": str(sub)},
        repository_extra_roots=str(project),
    )
    assert ok_sub == "ok-orch"
    assert Path(orch_calls[-1]["project"]) == sub.resolve()


# --- HostObservationLedger sentinel sanitization -----------------------------


def test_ledger_safe_tool_arguments_prevents_text_patch_poisoning(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    scoped = server._project_scope_args(
        "text_patch", {"root": ".", "patch": "--- a/x\n+++ b/x\n"}, str(project),
    )
    assert scoped["approval"] is server._TRUSTED_REPOSITORY_APPROVAL

    sanitized = server._ledger_safe_tool_arguments(
        {**scoped, "score": float("nan"), "nested": {"ok": 1, "bad": server._TRUSTED_REPOSITORY_APPROVAL}},
    )
    assert "approval" not in sanitized
    assert sanitized["score"] is None
    assert sanitized["nested"] == {"ok": 1}
    # Must serialize strictly with allow_nan=False and never poison HostObservationLedger.
    json.dumps(sanitized, allow_nan=False)
    ledger = HostObservationLedger(project_scope=str(project))
    ledger.observe(
        tool="text_patch",
        arguments=sanitized,
        observation="applied",
        dispatched=True,
        success=True,
        dirty=True,
    )
    assert ledger._poisoned is False


# --- Speculation guards (allowed_tools & tool_policy) ------------------------


def test_speculation_skips_when_tool_policy_set_or_tool_not_in_allowed_tools(
    monkeypatch, tmp_path,
):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")

    speculated = []

    class FakeSpecEngine:
        def __init__(self, predictor, dispatch, enabled=True):
            self.enabled = enabled

        def begin(self, tool, sig, args):
            if not self.enabled:
                return False
            speculated.append((tool, args))
            return True

        def retire(self, sig):
            return None

        def discard(self):
            return None

        def squash(self):
            return None

        def stats(self):
            return {}

        def close(self):
            return None

    class FakePredictor:
        def predict_next_tool(self, state):
            return ("directory_tree", 0.99)

        def should_speculate(self, confidence):
            return True

        def observe_decision_latency(self, seconds):
            return None

        def observe_tool_latency(self, seconds):
            return None

        def observe_outcome(self, *a, **k):
            return None

    class FakePrefetcher:
        def observe_listing(self, *a, **k):
            return None

        def predict_read(self):
            return {"path": "a.py"}

    monkeypatch.setattr(server.sonder_speculation, "speculation_enabled", lambda: True)
    monkeypatch.setattr(server.sonder_speculation, "default_predictor", lambda: FakePredictor())
    monkeypatch.setattr(server.sonder_speculation, "SpeculationEngine", FakeSpecEngine)
    monkeypatch.setattr(server.sonder_speculation, "FilePrefetcher", FakePrefetcher)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target", lambda tier, strict=None: ("fake-model", False, False, "code"))
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: (lambda p, h=None: '{"final": "done"}'))
    monkeypatch.setattr(server, "_auto_model_context", lambda m: None)

    # 1. With tool_policy set, speculation is disabled entirely.
    server._agent_turn(
        "inspect", project=str(project), read_only=True, max_steps=1,
        tool_policy=lambda name, args: "",
    )
    assert speculated == []

    # 2. With allowed_tools excluding both directory_tree and file_read, nothing speculates.
    server._agent_turn(
        "inspect", project=str(project), read_only=True, max_steps=1,
        tool_allowlist=("status",),
    )
    assert speculated == []

    # 3. With allowed_tools including only file_read, arg-free directory_tree is skipped
    #    and only allowed file_read prefetch runs.
    server._agent_turn(
        "inspect", project=str(project), read_only=True, max_steps=1,
        tool_allowlist=("file_read",),
    )
    assert [tool for tool, _ in speculated] == ["file_read"]


# --- post_status -> report_progress alias canonicalization -------------------


def test_post_status_canonicalizes_to_report_progress():
    assert server._AGENT_TOOL_ALIASES is tool_naming.AGENT_TOOL_ALIASES
    assert server._canonical_agent_tool_name is tool_naming.canonical_agent_tool_name
    assert server._canonical_agent_tool_name("post_status") == "report_progress"
    assert server._AGENT_TOOL_ALIASES["post_status"] == "report_progress"
