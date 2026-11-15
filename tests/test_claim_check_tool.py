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
