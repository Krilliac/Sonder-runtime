"""The mutating file family runs through the typed tool gateway on every surface.

``file_write``, ``file_edit``, ``directory_create``, ``file_copy``,
``file_move``, ``file_batch_write``, ``json_patch``, ``text_patch`` and
``file_delete`` reach the guarded primitives only through
``application.tools`` -- from the native MCP surface directly, and from the
legacy ``server`` handlers through ``server._typed_tool``. One pipeline
(schema, resource policy, deadline and cancellation, the packaged guards,
redaction) and one durable receipt per call whatever the outcome.

The permission decision is made exactly once per call: by the gateway for
the native surface (with the call's arguments, so an unattended refusal
names the call and a one-shot approval can answer the retry), and by the
legacy surface's own gate for a legacy forward, which the gateway records as
``permission:surface`` and does not repeat. The legacy handlers keep their
output formats byte for byte, structured refusals included.
"""
from __future__ import annotations

import io
import json

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.bootstrap.native_mcp import run_native_mcp
from sonder_runtime.platform import paths as runtime_paths

pytestmark = pytest.mark.unit

PATCH = "--- a/patched.txt\n+++ b/patched.txt\n@@ -1,3 +1,3 @@\n-needle\n+thread\n second line\n third\n"
BAD_PATCH = "--- a/patched.txt\n+++ b/patched.txt\n@@ -1,1 +1,1 @@\n-nomatch\n+x\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("notes.txt", "notes2.txt", "patched.txt", "patched2.txt"):
        (root / name).write_text("needle\nsecond line\nthird\n", encoding="utf-8")
    for name in ("m1.txt", "m2.txt", "del1.txt", "del2.txt"):
        (root / name).write_text("bytes\n", encoding="utf-8")
    for name in ("data.json", "data2.json"):
        (root / name).write_text('{"a": 1}\n', encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    return root


@pytest.fixture
def application(tmp_path, monkeypatch, workspace):
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    app = bootstrap_app.build_application()
    monkeypatch.setattr(server, "_APP_GRAPH", app)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    try:
        yield app
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def _audit(tmp_path):
    return DurableToolAuditRepository(tmp_path / "home" / "audit" / "tool-receipts.jsonl")


def _native_rows(app, name, arguments):
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO(json.dumps(initialize) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _native(app, name, arguments):
    return _native_rows(app, name, arguments)[1]["result"]


# (legacy call, native tool name, native arguments, canonical typed name)
CASES = [
    (lambda: server.file_write("out1.txt", "hello"),
     "file_write", {"path": "out2.txt", "content": "hello", "mode": "create"}, "write_file"),
    (lambda: server.file_edit("notes.txt", "needle", "thread"),
     "file_edit", {"path": "notes2.txt", "old": "needle", "new": "thread", "count": 1}, "edit_file"),
    (lambda: server.directory_create("d1/inner"),
     "directory_create", {"path": "d2/inner", "parents": True}, "make_directory"),
    (lambda: server.file_copy("notes.txt", "copy1.txt"),
     "file_copy", {"source": "notes.txt", "destination": "copy2.txt", "overwrite": False},
     "file_copy"),
    (lambda: server.file_move("m1.txt", "moved1.txt"),
     "file_move", {"source": "m2.txt", "destination": "moved2.txt", "overwrite": False},
     "file_move"),
    (lambda: server.file_batch_write(json.dumps(
        [{"path": "b1.txt", "content": "one", "mode": "create"}])),
     "file_batch_write", {"operations": [{"path": "b2.txt", "content": "two", "mode": "create"}]},
     "file_batch_write"),
    (lambda: server.json_patch("data.json", '[{"op": "replace", "path": "/a", "value": 2}]',
                               mode="apply"),
     "json_patch", {"path": "data2.json", "operations": [{"op": "replace", "path": "/a", "value": 2}],
                    "mode": "apply"}, "json_patch"),
    (lambda: server.text_patch(".", PATCH),
     "text_patch", {"root": ".", "patch": PATCH, "apply": False}, "text_patch"),
]


@pytest.mark.parametrize("legacy, native_name, native_args, canonical", CASES,
                         ids=[case[1] for case in CASES])
def test_both_surfaces_run_one_pipeline_and_leave_one_receipt_each(
    application, tmp_path, unattended_effects_allowed, legacy, native_name, native_args, canonical,
):
    out = legacy()
    native = _native(application, native_name, native_args)

    assert not out.startswith("ERROR"), out
    assert native["isError"] is False, native
    assert native["evidence"]["terminal"] == "completed"

    records = _audit(tmp_path).read()
    assert [record["tool_name"] for record in records] == [canonical, canonical]
    by_source = {record["source"]: record for record in records}
    assert set(by_source) == {"repl", "mcp"}
    assert by_source["repl"]["policy_match"] == "resource:mutating:%s;permission:surface" % canonical
    assert by_source["mcp"]["policy_match"] == "resource:mutating:%s;permission:mode" % canonical
    for record in records:
        assert record["terminal"] == "completed" and record["success"] is True
        assert record["model"] == ""
    _audit(tmp_path).verify()


def test_file_delete_runs_on_both_surfaces_dry_run_first(
    application, tmp_path, workspace, unattended_effects_allowed, every_tool_allowed_by_rule,
):
    out = server.file_delete("del1.txt")
    assert out.startswith("file delete") and "required_confirm: DELETE" in out
    assert (workspace / "del1.txt").exists()
    confirmed = server.file_delete("del1.txt", dry_run=False, confirm="DELETE %s" % (workspace / "del1.txt"))
    assert "deleted: True" in confirmed and not (workspace / "del1.txt").exists()

    native = _native(application, "file_delete", {"path": "del2.txt", "dry_run": True})
    assert native["isError"] is False and json.loads(native["output"])["dry_run"] is True
    assert (workspace / "del2.txt").exists()
    records = _audit(tmp_path).read()
    assert [record["tool_name"] for record in records] == ["file_delete"] * 3
    assert records[-1]["policy_match"] == "resource:mutating:file_delete;permission:rule"


def test_the_legacy_outputs_keep_their_formats(application, workspace):
    written = server.file_write("fmt.txt", "a\nb\n")
    assert written.splitlines()[0] == "file write"
    assert any(line.strip().startswith("path: ") for line in written.splitlines())
    assert (workspace / "fmt.txt").read_text(encoding="utf-8") == "a\nb\n"

    edited = server.file_edit("notes.txt", "needle", "thread")
    assert edited.startswith("file edit") and "replacements: 1" in edited

    batch = json.loads(server.file_batch_write(json.dumps(
        [{"path": "fmt2.txt", "content": "x", "mode": "create"}])))
    assert batch["count"] == 1 and batch["results"][0]["status"] == "written"

    preview = json.loads(server.json_patch("data.json", '[{"op": "replace", "path": "/a", "value": 3}]'))
    assert preview["mode"] == "preview" and preview["applied"] is False
    assert json.loads((workspace / "data.json").read_text(encoding="utf-8")) == {"a": 1}

    patched = json.loads(server.text_patch(".", PATCH))
    assert [row["path"] for row in patched["files"]] == ["patched.txt"]
    assert (workspace / "patched.txt").read_text(encoding="utf-8").startswith("needle")

    made = server.directory_create("fresh")
    assert made.startswith("directory create") and (workspace / "fresh").is_dir()


def test_structured_refusals_render_as_they_always_did_on_both_surfaces(
    application, tmp_path, monkeypatch, unattended_effects_allowed,
):
    duplicate = [{"path": "dup.txt", "content": "a", "mode": "create"},
                 {"path": "dup.txt", "content": "b", "mode": "create"}]
    legacy = server.file_batch_write(json.dumps(duplicate))
    assert legacy.startswith("ERROR: {")
    report = json.loads(legacy[len("ERROR: "):])
    assert report.get("ok") is False or "error" in report
    native = _native(application, "file_batch_write", {"operations": duplicate})
    assert native["isError"] is True and native["error"] == "BatchWriteError"
    assert json.loads(native["output"]).keys() == report.keys()

    # The JSON patch primitive raises its structured error only when the
    # atomic apply itself fails; stand in for that failure at the primitive.
    from sonder_runtime.adapters.filesystem import json_patch as json_patch_tool

    def failing_patch(*args, **kwargs):
        raise json_patch_tool.JsonPatchError(
            "atomic JSON patch failed", {"ok": False, "error": "disk full", "path": "data.json"})

    monkeypatch.setattr(json_patch_tool, "patch_json", failing_patch)
    ops = '[{"op": "replace", "path": "/a", "value": 1}]'
    legacy = server.json_patch("data.json", ops, mode="apply")
    assert legacy.startswith("ERROR: {")
    assert json.loads(legacy[len("ERROR: "):]) == {"ok": False, "error": "disk full", "path": "data.json"}
    native = _native(application, "json_patch", {"path": "data2.json", "operations": json.loads(ops),
                                                 "mode": "apply"})
    assert native["isError"] is True and native["error"] == "JsonPatchError"
    assert json.loads(native["output"])["error"] == "disk full"

    # A hunk that does not match is a plain refusal on both surfaces, as it
    # always was; the structured error is the transactional apply's.
    legacy = server.text_patch(".", BAD_PATCH)
    assert legacy == "ERROR: hunk context does not exactly match source"
    native = _native(application, "text_patch", {"root": ".", "patch": BAD_PATCH})
    assert native["isError"] is True and native["output"] == legacy[len("ERROR: "):]

    from sonder_runtime.adapters.filesystem import text_patch as text_patch_module

    def failing_apply(*args, **kwargs):
        raise text_patch_module.TextPatchError("text patch apply failed", {"ok": False, "error": "rolled back"})

    monkeypatch.setattr(text_patch_module, "text_patch", failing_apply)
    legacy = server.text_patch(".", PATCH, apply=True)
    assert legacy.startswith("ERROR: {") and json.loads(legacy[len("ERROR: "):])["error"] == "rolled back"
    native = _native(application, "text_patch", {"root": ".", "patch": PATCH, "apply": True})
    assert native["isError"] is True and native["error"] == "TextPatchError"

    records = _audit(tmp_path).read()
    assert len(records) == 8
    assert {record["terminal"] for record in records} == {"failed"}
    assert {record["error_code"] for record in records} == {
        "BatchWriteError", "JsonPatchError", "TextPatchError", "ValueError"}


def test_containment_is_identical_on_both_surfaces(
    application, tmp_path, unattended_effects_allowed,
):
    outside = str(tmp_path / "outside.txt")
    legacy = server.file_write(outside, "x")
    native = _native(application, "file_write", {"path": outside, "content": "x", "mode": "create"})
    assert legacy.startswith("ERROR: ") and native["isError"] is True
    assert legacy[len("ERROR: "):] == native["output"]
    assert not (tmp_path / "outside.txt").exists()

    legacy = server.file_edit(".env", "a", "b")
    native = _native(application, "file_edit", {"path": ".env", "old": "a", "new": "b"})
    assert legacy.startswith("ERROR: ") and native["isError"] is True
    assert legacy[len("ERROR: "):] == native["output"]
    records = _audit(tmp_path).read()
    assert [record["terminal"] for record in records] == ["failed"] * 4


def test_the_native_schema_still_refuses_the_guard_knobs(application):
    for name, arguments in (
        ("file_write", {"path": "x.txt", "content": "y", "bypass": True}),
        ("write_file", {"path": "x.txt", "content": "y", "token": "t"}),
        ("make_directory", {"path": "d", "developer_authorized": True}),
        ("file_delete", {"path": "x.txt", "approval": "code"}),
    ):
        row = _native_rows(application, name, arguments)[1]
        assert row.get("error", {}).get("code") == -32602, (name, row)


def test_native_calls_are_decided_once_and_legacy_forwards_not_again(
    application, tmp_path, monkeypatch, unattended_effects_allowed,
):
    seen = []
    real = pm.decide_for_caller

    def spy(tool_name, **kwargs):
        seen.append((tool_name, kwargs.get("surface"), kwargs.get("arguments")))
        return real(tool_name, **kwargs)

    monkeypatch.setattr(pm, "decide_for_caller", spy)

    arguments = {"path": "once.txt", "content": "x", "mode": "create"}
    assert _native(application, "file_write", arguments)["isError"] is False
    assert seen == [("file_write", "native-mcp", arguments)]

    seen.clear()
    assert not server.file_write("twice.txt", "x").startswith("ERROR")
    assert seen == [], "an internal call is deliberately ungated and the gateway does not decide it"
    assert _audit(tmp_path).read()[-1]["policy_match"].endswith("permission:surface")

    seen.clear()
    out = server.control_command("/file_write path=thrice.txt content=x")
    assert not out.startswith("refused") and not out.startswith("ERROR")
    assert [(name, surface) for name, surface, _args in seen] == [("file_write", "control")]
    assert seen[0][2] == {"path": "thrice.txt", "content": "x"}
    assert _audit(tmp_path).read()[-1]["policy_match"].endswith("permission:surface")


def test_an_unattended_native_mutation_is_refused_with_a_call_id_then_approved_once(
    application, tmp_path, monkeypatch,
):
    ledger = ledger_module.ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    arguments = {"path": "approved.txt", "content": "x", "mode": "create"}

    refused = _native(application, "file_write", arguments)
    assert refused["isError"] is True and refused["error"] == "permission_denied"
    call = refused["evidence"]["call_id"]
    assert call and refused["evidence"]["source"] == "unattended"
    assert "/approve %s" % call in refused["output"]
    pending = ledger.pending()
    assert [item.call_id for item in pending] == [call]
    assert pending[0].tool == "file_write" and pending[0].surface == "native-mcp"
    assert _audit(tmp_path).read()[-1]["terminal"] == "policy_denied"

    ledger.issue("file_write", pending[0].digest, approver="console operator")
    allowed = _native(application, "file_write", arguments)
    assert allowed["isError"] is False, allowed
    assert _audit(tmp_path).read()[-1]["policy_match"] == "resource:mutating:write_file;permission:approval"
    assert ledger.pending() == [] and ledger.approvals() == []

    again = _native(application, "file_write", {**arguments, "mode": "overwrite"})
    assert again["isError"] is True and again["evidence"]["source"] == "unattended"


def test_plan_mode_refuses_every_mutation_on_the_native_surface(application, monkeypatch):
    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    refused = _native(application, "file_write", {"path": "p.txt", "content": "x"})
    assert refused["isError"] is True and refused["evidence"]["source"] == "mode"
