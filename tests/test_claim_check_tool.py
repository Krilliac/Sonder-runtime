"""The claim_check MCP tool: shared executor, legacy registrar and native descriptor."""
import json
from dataclasses import replace

import pytest

from sonder_runtime.bootstrap import claim_check_agent_tools as tool
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig

PUMP = "class P {\n  void Fill(int n) {\n    var a = new byte[n];\n  }\n}\n"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "P.cs").write_text(PUMP, newline="\n")
    (root / "secret.cs").write_text(PUMP, newline="\n")
    return root


def allow_all(path):
    return path


def test_single_claim_returns_one_typed_report(repo):
    out = tool.run_claim_check({"root": str(repo), "file": "P.cs", "line": 99},
                               root_guard=allow_all)
    assert out["ok"] is True
    assert out["summary"]["verdicts"] == {"reject": 1, "flag": 0, "keep": 0}
    report = out["reports"][0]
    assert report["verdict"] == "reject"
    assert report["checks"][1]["name"] == "line_in_range"


def test_claims_json_batch_collapses_duplicates(repo):
    claims = [{"id": "a", "file": "P.cs", "line": 3, "symbols": ["Fill"]},
              {"id": "b", "file": "P.cs", "line": 4}]
    out = tool.run_claim_check({"root": str(repo), "claims_json": json.dumps(claims)},
                               root_guard=allow_all)
    assert [r["duplicate_of"] for r in out["reports"]] == ["", "a"]
    assert out["summary"]["duplicates"] == 1 and out["summary"]["total"] == 2


def test_prose_fills_missing_citations(repo):
    out = tool.run_claim_check(
        {"root": str(repo), "file": "P.cs", "claim_text": "Line 3 allocates `Fill` unchecked"},
        root_guard=allow_all)
    assert out["reports"][0]["line_start"] == 3


@pytest.mark.parametrize("arguments,code", [
    ({}, "invalid_arguments"),
    ({"root": "x"}, "invalid_arguments"),
    ({"root": "x", "claims_json": "not json"}, "invalid_arguments"),
    ({"root": "x", "claims_json": "{}"}, "invalid_arguments"),
    ({"root": "x", "claims_json": "[1]"}, "invalid_claim"),
    ({"root": "x", "file": ""}, "invalid_claim"),
    ({"root": "x", "file": "a.cs", "line": 0, "line_end": 5}, "invalid_claim"),
])
def test_bad_arguments_are_typed_errors_not_exceptions(arguments, code):
    out = tool.run_claim_check(arguments, root_guard=allow_all)
    assert out["ok"] is False and out["error"]["code"] == code


def test_too_many_claims_is_a_typed_error(repo):
    cfg = replace(ClaimCheckConfig(), max_batch_claims=2)
    claims = [{"file": "P.cs", "line": 1}] * 3
    out = tool.run_claim_check({"root": str(repo), "claims_json": json.dumps(claims)},
                               config=cfg, root_guard=allow_all)
    assert out["error"]["code"] == "too_many_claims"


def test_root_outside_authorized_roots_is_refused(repo):
    def refuse(path):
        raise PermissionError("not an authorized root")

    out = tool.run_claim_check({"root": str(repo), "file": "P.cs", "line": 1}, root_guard=refuse)
    assert out["ok"] is False and out["error"]["code"] == "root_refused"
    assert "authorized" in out["error"]["message"]


def test_missing_root_directory_is_refused(tmp_path):
    out = tool.run_claim_check({"root": str(tmp_path / "nope"), "file": "P.cs", "line": 1},
                               root_guard=allow_all)
    assert out["error"]["code"] == "root_refused"


def test_read_guard_hides_sensitive_files(repo):
    def deny_secret(path):
        if path.endswith("secret.cs"):
            raise PermissionError("sensitive")
        return path

    out = tool.run_claim_check(
        {"root": str(repo), "claims_json": json.dumps([{"file": "secret.cs", "line": 99}])},
        root_guard=allow_all, file_guard=deny_secret)
    report = out["reports"][0]
    missing = tool.run_claim_check(
        {"root": str(repo), "claims_json": json.dumps([{"file": "absent.cs", "line": 99}])},
        root_guard=allow_all, file_guard=deny_secret)["reports"][0]
    assert report["verdict"] == missing["verdict"] == "reject"
    assert report["checks"] == missing["checks"]


def test_truncation_serializes_each_report_once(repo, monkeypatch):
    calls = []
    real_dumps = json.dumps

    def counting(*args, **kwargs):
        calls.append(1)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(tool.json, "dumps", counting)
    monkeypatch.setattr(tool, "MAX_OUTPUT_CHARS", 3_000)
    count = 400
    claims = [{"id": str(i), "file": "P.cs", "line": 99} for i in range(count)]
    config = replace(ClaimCheckConfig(), max_batch_claims=count)
    out = tool.run_claim_check({"root": str(repo), "claims_json": real_dumps(claims)},
                               config=config, root_guard=allow_all)
    omitted = out["summary"]["omitted_reports"]
    assert 0 < omitted < count and len(out["reports"]) == count - omitted
    assert len(calls) <= len(out["reports"]) + 1  # work is bounded by what is kept
    assert len(real_dumps(out["reports"], ensure_ascii=False)) <= 3_000


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def test_registrar_exposes_a_json_tool_and_records_the_call(repo):
    mcp, calls = FakeMcp(), []
    tool.register(mcp, lambda name, args, **kw: calls.append((name, kw["ok"])),
                  root_guard=allow_all, config_provider=lambda: ClaimCheckConfig())
    text = mcp.tools["claim_check"](root=str(repo), file="P.cs", line=99)
    assert json.loads(text)["reports"][0]["verdict"] == "reject"
    assert calls == [("claim_check", True)]


def test_registrar_is_discovered_without_server_edits():
    from sonder_runtime.bootstrap.agent_help_tools import discover_agent_tool_registrars

    assert "claim_check_agent_tools" in dict(discover_agent_tool_registrars())


def test_native_catalog_lists_a_read_only_claim_check():
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry
    from sonder_runtime.domain.tools.descriptors import ToolEffect
    from sonder_runtime.domain.tools.traits import TriState

    descriptor = native_tool_registry().get("claim_check")
    assert descriptor is not None
    assert descriptor.input_schema["required"] == ["root"]
    assert ToolEffect.READ_FILES in descriptor.effects
    assert descriptor.traits.read_only == TriState.TRUE


def test_native_handler_returns_the_same_json(repo):
    out = tool.native_claim_check(
        {"root": str(repo), "file": "P.cs", "line": 99}, root_guard=allow_all)
    assert out["isError"] is False and out["evidence"] == {"tool": "claim_check"}
    assert json.loads(out["output"])["summary"]["verdicts"]["reject"] == 1
    failed = tool.native_claim_check({"root": str(repo)}, root_guard=allow_all)
    assert failed["isError"] is True and failed["error"] == "tool_refused"


def _frame_for(result, limit_bytes=None):
    """The bytes the stdio transport writes for this native tool result."""
    from sonder_runtime.interfaces.mcp.transport import StdioMcpTransport

    standard = StdioMcpTransport._standard_tool_result(result)
    encoded = json.dumps({"jsonrpc": "2.0", "id": 1, "result": standard},
                         separators=(",", ":"), ensure_ascii=True)
    return len(encoded.encode("utf-8"))


def test_large_native_batch_fits_the_transport_frame_and_reports_the_cut(tmp_path):
    from sonder_runtime.interfaces.mcp.transport import McpTransportLimits

    root = tmp_path / "repo"
    root.mkdir()
    count = 600
    for index in range(count):
        (root / f"f{index}.cs").write_text(PUMP, newline="\n")
    claims = [{"id": f"claim-\u00e9\u4e2d-{index}", "file": f"f{index}.cs", "line": 99}
              for index in range(count)]
    result = tool.native_claim_check(
        {"root": str(root), "claims_json": json.dumps(claims)},
        root_guard=allow_all, file_guard=allow_all)
    limit = McpTransportLimits().max_frame_bytes
    frame = _frame_for(result)
    assert frame <= limit
    assert frame > limit * 0.9  # the cut keeps as much as fits, not a token amount
    out = json.loads(result["output"])
    omitted = out["summary"]["omitted_reports"]
    assert 0 < omitted < count and len(out["reports"]) == count - omitted
    assert out["summary"]["total"] == count and result["isError"] is False


def test_small_native_batch_is_not_truncated(repo):
    result = tool.native_claim_check({"root": str(repo), "file": "P.cs", "line": 99},
                                     root_guard=allow_all, file_guard=allow_all)
    out = json.loads(result["output"])
    assert "omitted_reports" not in out["summary"] and len(out["reports"]) == 1


def test_native_cut_honours_an_explicit_frame_limit(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    count = 50
    for index in range(count):
        (root / f"f{index}.cs").write_text(PUMP, newline="\n")
    claims = [{"file": f"f{index}.cs", "line": 99} for index in range(count)]
    result = tool.native_claim_check(
        {"root": str(root), "claims_json": json.dumps(claims)},
        root_guard=allow_all, file_guard=allow_all, max_frame_bytes=20_000)
    assert _frame_for(result) <= 20_000
    assert json.loads(result["output"])["summary"]["omitted_reports"] > 0


def test_read_policy_snapshot_resolves_the_control_plane_once_and_still_refuses(tmp_path, monkeypatch):
    from sonder_runtime.adapters.filesystem import file_ops

    (tmp_path / "ok.cs").write_text("x\n")
    (tmp_path / "sonder.env").write_text("KEY=1\n")
    inventories = []
    real = file_ops.live_control_plane_inventory

    def counting(*args, **kwargs):
        inventories.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(file_ops, "live_control_plane_inventory", counting)
    ok = str(tmp_path / "ok.cs")
    for _ in range(5):
        file_ops.resolve_repository_read_path(ok, extra_roots=str(tmp_path))
    unscoped = len(inventories)
    inventories.clear()
    with file_ops.read_policy_snapshot():
        for _ in range(5):
            file_ops.resolve_repository_read_path(ok, extra_roots=str(tmp_path))
        with pytest.raises(PermissionError):
            file_ops.resolve_repository_read_path(
                str(tmp_path / "sonder.env"), extra_roots=str(tmp_path))
    assert unscoped >= 5 and len(inventories) <= 2
    assert file_ops._POLICY_SNAPSHOT.get() is None


def _executed_frame(result, registry_names):
    """The frame for ``result`` after native execute() adds its progressive-tools evidence."""
    from sonder_runtime.application.ports.tool_registry import ToolSchemaSelection

    longest = tool._MAX_TOOL_NAME_CHARS
    names = {str(index).ljust(longest, "z") for index in range(8)}
    evidence = dict(result["evidence"])
    evidence.update({"tool_schema_selection": ToolSchemaSelection(
        frozenset(names), selection_id="f" * 64).marker(),
        "inventory_digest": "e" * 64, "native_compatibility_path": True})
    return _frame_for({**result, "evidence": evidence})


def test_native_cut_covers_the_evidence_execute_adds_after_the_handler(tmp_path):
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry

    registry_names = [item.name for item in native_tool_registry().list_all()]
    assert max(map(len, registry_names)) <= tool._MAX_TOOL_NAME_CHARS
    root = tmp_path / "repo"
    root.mkdir()
    count = 60
    for index in range(count):
        (root / f"f{index}.cs").write_text(PUMP, newline="\n")
    limit = 20_000
    worst_slack = limit
    # Claim-id length moves the report size, so some length leaves almost no slack.
    for pad in range(1, 90):
        claims = [{"id": ("c%d-" % index).ljust(pad, "q"), "file": f"f{index}.cs", "line": 99}
                  for index in range(count)]
        result = tool.native_claim_check(
            {"root": str(root), "claims_json": json.dumps(claims)},
            root_guard=allow_all, file_guard=allow_all, max_frame_bytes=limit)
        frame = _executed_frame(result, registry_names)
        assert frame <= limit, "id length %d overflows by %d bytes" % (pad, frame - limit)
        worst_slack = min(worst_slack, limit - frame)
    # the sweep reached frames that nearly fill the limit (the envelope reserve is all that is left)
    assert worst_slack < 1_024 + 400


def test_default_guard_opens_the_policy_snapshot_scope_once(repo, monkeypatch):
    from sonder_runtime.adapters.filesystem import file_ops

    for index in range(6):
        (repo / f"g{index}.cs").write_text(PUMP, newline="\n")
    claims = [{"file": f"g{index}.cs", "line": 3} for index in range(6)]
    args = {"root": str(repo), "claims_json": json.dumps(claims)}
    inventories = []
    real = file_ops.live_control_plane_inventory

    def counting(*a, **kw):
        inventories.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(file_ops, "live_control_plane_inventory", counting)
    scoped = tool.run_claim_check(args, root_guard=allow_all)
    assert scoped["ok"] is True and scoped["summary"]["total"] == 6
    scoped_calls = len(inventories)
    inventories.clear()
    tool.run_claim_check(args, root_guard=allow_all, file_guard=lambda path: str(
        file_ops.resolve_repository_read_path(path, extra_roots=str(repo))))
    assert scoped_calls <= 2 < len(inventories)


@pytest.mark.parametrize("fault", [OSError("disk"), ValueError("bad"), PermissionError("no")])
def test_a_fault_during_the_checks_is_not_labelled_root_refused(repo, monkeypatch, fault):
    from sonder_runtime.adapters.claim_checker import ClaimChecker

    def boom(self, claims):
        raise fault

    monkeypatch.setattr(ClaimChecker, "check_many", boom)
    with pytest.raises(type(fault)):
        tool.run_claim_check({"root": str(repo), "file": "P.cs", "line": 3}, root_guard=allow_all)
    registered = {}

    class Mcp:
        def tool(self):
            return lambda fn: registered.setdefault("fn", fn)

    tool.register(Mcp(), None, root_guard=allow_all, file_guard=allow_all)
    out = json.loads(registered["fn"](root=str(repo), file="P.cs", line=3))
    assert out["error"]["code"] == "internal_error"


def test_a_refused_root_is_still_root_refused(repo):
    def refuse(root):
        raise PermissionError("outside")

    out = tool.run_claim_check({"root": str(repo), "file": "P.cs", "line": 3}, root_guard=refuse)
    assert out["error"]["code"] == "root_refused"
