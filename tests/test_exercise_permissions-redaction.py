"""Lane 6 (permissions-redaction): F21 redaction, F09 session modes, F20 deletes,
F19 refusal text and F14 read-only lane gaps."""
from __future__ import annotations

import json
import os
import random
import re
from types import SimpleNamespace

import pytest

import permission_modes as pm
import permission_rules
import server
from sonder_runtime.adapters import chat_run_backend
from sonder_runtime.adapters.filesystem import file_ops, read_only_root_gate
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.application.tools.facade import PatternOutputRedactor
from sonder_runtime.application.tools.gateway_contract import (
    ApprovalMode, ToolGateway, ToolGatewayRequest, ToolInvocationOutput,
    ToolPermission, ToolScope,
)
from sonder_runtime.domain.execution import policy as exec_policy
from sonder_runtime.domain.security import delete_calls, redaction
from sonder_runtime.interfaces.http import chat_tool_stream

pytestmark = pytest.mark.unit


# --- F21: structured redaction of JSON tool output ---------------------------

class _Schema:
    def validate(self, name, arguments):
        return None


class _Permissions:
    def authorize(self, name, scope, permission):
        return None


class _Approvals:
    def approve(self, request):
        return True


class _Invoker:
    def __init__(self, output):
        self._output = output

    def invoke(self, request):
        return ToolInvocationOutput(True, self._output)


class _Receipts:
    def __init__(self):
        self.items = []

    def record(self, receipt):
        self.items.append(receipt)


def _run_gateway(output, redactor=None):
    receipts = _Receipts()
    gateway = ToolGateway(
        _Schema(), _Permissions(), _Approvals(), _Invoker(output),
        redactor or PatternOutputRedactor(), receipts,
    )
    request = ToolGatewayRequest(
        request_id="r1", tool_name="text_search", arguments={"query": "x"},
        scope=ToolScope("owner", ("project",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"}), ApprovalMode.NOT_REQUIRED),
    )
    return gateway.execute(request).output


SECRET_LINES = [
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("token: abcdefgh", "abcdefgh"),
    ("db_password=hunter22", "hunter22"),
    ('password = "hunter22"', "hunter22"),
]


@pytest.mark.parametrize("text, secret", SECRET_LINES)
def test_json_tool_output_is_redacted_and_still_parses(text, secret):
    payload = {"matches": [{"path": "a.env", "line": 3, "text": text}], "count": 1}
    out = _run_gateway(json.dumps(payload, sort_keys=True))
    parsed = json.loads(out)  # must not raise: the closing quote survives
    assert secret not in out
    assert parsed["count"] == 1 and parsed["matches"][0]["path"] == "a.env"
    assert "[REDACTED]" in parsed["matches"][0]["text"]


def test_redact_text_still_covers_unbalanced_and_plain_values():
    assert "hunter22" not in redaction.redact_text('password="hunter22')
    assert "hunter22" not in redaction.redact_text("password: 'hunter22'")
    assert "hunter22" not in redaction.redact_text("password=hunter22")


def test_non_json_output_is_still_redacted_as_text():
    out = _run_gateway("db_password=hunter22 done")
    assert "hunter22" not in out


# A secret held under a credential key: the key gave the text pattern its
# context, so a structural redactor that sees value and key apart must still
# withhold it (production composition: PatternOutputRedactor(Redactor().redact)).
KEYED_SECRETS = [
    ({"password": "hunter22xyz"}, "hunter22xyz"),
    ({"db_password": "hunter22xyz"}, "hunter22xyz"),
    ({"api_key": "abcdef123456"}, "abcdef123456"),
    ({"token": "abcdefgh"}, "abcdefgh"),
    ({"env": {"GITHUB_TOKEN": "ghp_abcdefghij1234"}}, "ghp_abcdefghij1234"),
    ({"config": {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}}, "wJalrXUtnFEMI"),
    ({"rows": [{"password": "hunter22xyz"}]}, "hunter22xyz"),
    ({"password": 12345678}, "12345678"),
    ({"credentials": {"user": "bobsmith", "pass": "hunter22xyz"}}, "bobsmith"),
    ({"AKIAIOSFODNN7EXAMPLE": 1}, "AKIAIOSFODNN7EXAMPLE"),
]


def _production_redactor():
    from sonder_runtime.platform.logging import Redactor
    return PatternOutputRedactor(Redactor().redact)


@pytest.mark.parametrize("make", [PatternOutputRedactor, _production_redactor])
@pytest.mark.parametrize("payload, secret", KEYED_SECRETS)
def test_a_secret_under_a_credential_key_is_redacted_in_json_output(payload, secret, make):
    out = _run_gateway(json.dumps(payload), make())
    json.loads(out)  # still valid JSON
    assert secret not in out, out


def test_non_secret_json_fields_survive_key_aware_redaction():
    out = _run_gateway(json.dumps({"count": 3, "ok": True, "name": "alpha", "token": ""}))
    assert json.loads(out) == {"count": 3, "ok": True, "name": "alpha", "token": ""}


# --- F09: per-session permission modes ---------------------------------------

class _Handler:
    def __init__(self, session=""):
        self.headers = {"X-Sonder-Session": session} if session else {}
        self.sent = []

    def _send_json_payload(self, payload, status=200):
        self.sent.append((status, payload))


@pytest.fixture
def mode_state(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    saved = (dict(pm._STATE), dict(pm._SCOPED_MODES), pm._LOADED)
    pm._reset_for_tests()
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved[0])
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved[1])
        pm._LOADED = saved[2]


def _post(handler, body):
    chat_tool_stream.handle_permission_mode_post(
        handler, body, SimpleNamespace(),
        permission_policy=permission_policy,
        principal_of=lambda _ctx: "owner",
        idempotent_action=lambda _ctx, _key, _name, action: action(),
        send_idempotency_refusal=lambda _h, _r: False,
        observe_state=lambda: None,
        mode_data=lambda: {"mode": pm.current_mode()},
        approver_of=lambda _ctx: "owner",
    )
    return handler.sent[-1]


def test_two_sessions_keep_their_own_modes_under_the_global_ceiling(mode_state):
    status, payload = _post(_Handler(), {"mode": "auto", "scope": "global"})
    assert status == 200 and payload["scope"] == "global" and payload["mode"] == pm.AUTO
    status_a, payload_a = _post(_Handler("sess-A"), {"mode": "manual"})
    status_b, payload_b = _post(_Handler("sess-B"), {"mode": "acceptEdits"})
    assert (status_a, status_b) == (200, 200)
    assert payload_a["scope"] == "session" and payload_a["session_id"] == "sess-A"
    assert payload_a["mode"] == pm.MANUAL
    with pm.mode_scope("owner", "sess-A"):
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("owner", "sess-B"):
        assert pm.current_mode() == pm.ACCEPT_EDITS
    with pm.mode_scope("owner", "sess-C"):
        assert pm.current_mode() == pm.AUTO
    assert pm.global_mode() == pm.AUTO


def test_a_session_can_never_be_raised_above_the_global_mode(mode_state):
    assert pm.global_mode() == pm.MANUAL
    status, payload = _post(_Handler("sess-A"), {"mode": "auto"})
    assert status == 400 and "global" in payload["error"]
    with pm.mode_scope("owner", "sess-A"):
        assert pm.current_mode() == pm.MANUAL
    assert pm.global_mode() == pm.MANUAL


def test_body_session_id_and_scope_session_also_scope_the_change(mode_state):
    _post(_Handler(), {"mode": "auto", "scope": "global"})
    status, payload = _post(_Handler(), {"mode": "plan", "session_id": "sess-X"})
    assert status == 200 and payload["scope"] == "session"
    assert pm.global_mode() == pm.AUTO
    with pm.mode_scope("owner", "sess-X"):
        assert pm.current_mode() == pm.PLAN
    status, payload = _post(_Handler(), {"mode": "manual", "scope": "session"})
    assert status == 200 and payload["scope"] == "session"
    assert pm.global_mode() == pm.AUTO


def test_no_session_still_changes_the_global_mode(mode_state):
    status, payload = _post(_Handler(), {"mode": "acceptEdits"})
    assert status == 200 and payload["scope"] == "global"
    assert payload.get("session_id", "") == ""
    assert pm.global_mode() == pm.ACCEPT_EDITS


def test_an_unknown_scope_is_refused(mode_state):
    status, _payload = _post(_Handler(), {"mode": "manual", "scope": "galaxy"})
    assert status == 400


# --- F20: deletes always ask -------------------------------------------------

FILE_DELETE_ARGS = {"path": "notes/old.txt"}
HOST_DELETES = (
    ("workspace_run", {"program": "git", "args_json": json.dumps(["rm", "-f", "notes/old.txt"])}),
    ("workspace_run", {"program": "rm", "args_json": json.dumps(["-rf", "notes"])}),
    ("workspace_run", {"program": "cmd", "args_json": json.dumps(["/c", "del", "notes\\old.txt"])}),
    ("workspace_run", {"program": "powershell",
                       "args_json": json.dumps(["-Command", "Remove-Item notes/old.txt"])}),
    ("workspace_run", {"program": "git", "args_json": json.dumps(["clean", "-fd"])}),
    ("run_program", {"program": "git", "args_json": json.dumps(["rm", "x.txt"])}),
)


@pytest.fixture
def shipped_rules(tmp_path, monkeypatch):
    store = ledger_module.ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    lookup = permission_rules.rule_lookup(exec_policy.default_rules())
    pm.reset_unattended_for_tests()
    yield store, lookup
    pm.reset_unattended_for_tests()


def test_the_shipped_file_delete_rule_is_ask():
    rule = exec_policy.evaluate(exec_policy.default_rules(), "file_delete")
    assert rule["action"] == "ask"


def test_an_unmodified_old_shipped_row_migrates_but_a_custom_deny_survives(tmp_path):
    old = exec_policy.default_rules()
    for rule in old:
        if rule["pattern"] == "file_delete":
            rule.update(action="deny", note="destructive by default")
    permission_rules.save(tmp_path, old)
    rules, _report = permission_rules.load_report(tmp_path)
    assert exec_policy.evaluate(rules, "file_delete")["action"] == "ask"

    custom = exec_policy.default_rules()
    for rule in custom:
        if rule["pattern"] == "file_delete":
            rule.update(action="deny", note="owner wants no deletes")
    permission_rules.save(tmp_path, custom)
    rules, _report = permission_rules.load_report(tmp_path)
    assert exec_policy.evaluate(rules, "file_delete")["action"] == "deny"


@pytest.mark.parametrize("mode", [pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO])
def test_file_delete_and_host_deletes_always_ask(shipped_rules, mode):
    _store, lookup = shipped_rules
    verdicts = set()
    calls = [("file_delete", FILE_DELETE_ARGS), *HOST_DELETES]
    for name, args in calls:
        decision = pm.decide(name, interactive=False, mode=mode, rule_lookup=lookup,
                             arguments=args, surface="http")
        assert decision.action == pm.DENY and decision.source == "unattended", (name, args, decision)
        assert decision.call_id
        verdicts.add((decision.action, decision.source))
        interactive = pm.decide(name, interactive=True, mode=mode, rule_lookup=lookup,
                                arguments=args)
        assert interactive.action == pm.ASK, (name, args, interactive)
    assert len(verdicts) == 1


def test_bypass_alone_skips_the_delete_ask(shipped_rules):
    _store, lookup = shipped_rules
    # run_program is not in the permission catalog (grade "unclassified"), which
    # bypass never skips; its delete ask is covered by the always-ask test.
    host = [call for call in HOST_DELETES if call[0] != "run_program"]
    for name, args in [("file_delete", FILE_DELETE_ARGS), *host]:
        decision = pm.decide(name, interactive=False, mode=pm.BYPASS, rule_lookup=lookup,
                             arguments=args)
        assert decision.action == pm.ALLOW, (name, decision)


def test_an_explicit_allow_rule_does_not_waive_the_delete_ask(shipped_rules):
    allow = lambda _name: {"pattern": "*", "action": "allow", "note": ""}  # noqa: E731
    decision = pm.decide("file_delete", interactive=False, mode=pm.AUTO, rule_lookup=allow,
                         arguments=FILE_DELETE_ARGS, surface="http")
    assert decision.action == pm.DENY and decision.source == "unattended"


def test_an_ordinary_host_program_is_not_swept_into_the_delete_ask(shipped_rules):
    _store, lookup = shipped_rules
    decision = pm.decide(
        "workspace_run", interactive=False, mode=pm.AUTO, rule_lookup=lookup,
        arguments={"program": "git", "args_json": json.dumps(["status"])}, surface="http")
    assert decision.action == pm.ALLOW


def test_each_delete_needs_a_fresh_approval(shipped_rules):
    store, lookup = shipped_rules
    refused = pm.decide("file_delete", interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                        arguments=FILE_DELETE_ARGS, surface="http")
    store.issue("file_delete", pm.call_digest("file_delete", FILE_DELETE_ARGS), approver="owner")
    approved = pm.decide("file_delete", interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                         arguments=FILE_DELETE_ARGS, surface="http")
    assert approved.action == pm.ALLOW and approved.source == "approval"
    again = pm.decide("file_delete", interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                      arguments=FILE_DELETE_ARGS, surface="http")
    assert again.action == pm.DENY and again.source == "unattended"
    assert again.call_id == refused.call_id


def test_a_host_delete_approval_is_spent_after_one_use(shipped_rules):
    store, lookup = shipped_rules
    name, args = HOST_DELETES[0]
    pm.decide(name, interactive=False, mode=pm.AUTO, rule_lookup=lookup, arguments=args)
    store.issue(name, pm.call_digest(name, args), approver="owner")
    assert pm.decide(name, interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                     arguments=args).action == pm.ALLOW
    assert pm.decide(name, interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                     arguments=args).action == pm.DENY


def test_a_rule_refusal_names_the_rule_file_and_the_command(tmp_path, monkeypatch):
    import sonder_paths
    monkeypatch.setattr(sonder_paths, "default_home", lambda: tmp_path)
    permission_rules.save(tmp_path, [{"pattern": "file_write", "action": "deny", "note": "no"}])
    decision = pm.decide("file_write", interactive=False, mode=pm.AUTO, arguments={"path": "a"},
                         surface="agent", record=False)
    assert decision.source == "rule"
    assert str(permission_rules.policy_path(tmp_path)) in decision.reason
    assert "permission_rule_set" in decision.reason


def test_the_agent_refusal_does_not_say_choose_a_different_tool():
    for source in ("rule", "unattended", "mode"):
        decision = pm.Decision(pm.DENY, pm.AUTO, "dangerous", "because", "file_delete",
                               source=source, call_id="abcd1234efgh5678")
        text = chat_run_backend.format_agent_gate_refusal("file_delete", decision)
        assert "Choose a different tool" not in text
        assert "do not route around this refusal" in text.lower()


# --- F19: refusal text names the HTTP approval routes ------------------------

def test_an_http_unattended_refusal_names_the_approval_routes(shipped_rules, monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _t: None)
    decision = pm.decide("file_write", interactive=False, mode=pm.MANUAL,
                         arguments={"path": "a.txt", "content": "x"}, surface="http")
    assert decision.source == "unattended"
    assert "POST /v1/approvals/%s" % decision.call_id in decision.reason
    assert "POST /v1/approvals/deny/%s" % decision.call_id in decision.reason
    assert "do not route around this refusal" in decision.reason.lower()


def test_a_console_surface_keeps_the_console_wording(shipped_rules, monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _t: None)
    decision = pm.decide("file_write", interactive=False, mode=pm.MANUAL,
                         arguments={"path": "a.txt", "content": "x"}, surface="repl")
    assert "/approve %s" % decision.call_id in decision.reason
    assert "/v1/approvals" not in decision.reason


def test_the_agent_refusal_names_the_http_routes():
    decision = pm.Decision(pm.DENY, pm.MANUAL, "mutation", "r", "file_write",
                           source="unattended", call_id="abcd1234efgh5678")
    text = chat_run_backend.format_agent_gate_refusal("file_write", decision)
    assert "POST /v1/approvals/abcd1234efgh5678" in text
    assert "POST /v1/approvals/deny/abcd1234efgh5678" in text
    assert "/approve abcd1234efgh5678" in text


# --- F14: the read-only lanes admit the root-confined read tools -------------

def test_read_only_lane_admits_the_confined_read_tools():
    allowed = server.REPOSITORY_READ_ONLY_TOOLS
    for name in ("find_references", "diff_files", "tool_search"):
        assert name in allowed, name
    help_text = server.REPOSITORY_AGENT_TOOL_HELP
    for name in ("find_references", "diff_files"):
        assert "- %s:" % name in help_text, name
    # secret_scan follows in-root links to files outside the root and the gate
    # does not vet them, so it stays out of the lane, as at 7f95afccc.
    for name in ("secret_scan", "dependency_audit"):
        assert name not in allowed, name
        assert "- %s:" % name not in help_text, name
    assert "secret_scan" not in read_only_root_gate.ROOT_CONFINED_READ_TOOLS


def test_read_only_lane_refuses_secret_scan(monkeypatch):
    monkeypatch.chdir(file_ops.workspace_root())
    error = server._repository_read_only_error("secret_scan", {"root": "."})
    assert error == "ERROR: tool 'secret_scan' is not allowed by the repository read-only policy."


def test_autopilot_observe_lane_gets_the_reads_but_not_secret_scan():
    observe = server._AUTOPILOT_OBSERVE_TOOLS
    assert "find_references" in observe and "diff_files" in observe
    assert "secret_scan" not in observe and "dependency_audit" not in observe
    assert "dependency_audit" not in server._AUTOPILOT_WORKSPACE_TOOLS


def test_read_only_gate_passes_in_root_and_refuses_out_of_root(tmp_path, monkeypatch):
    monkeypatch.chdir(file_ops.workspace_root())  # "." means the same tree to tool and gate
    assert server._repository_read_only_error("find_references", {"root": ".", "symbol": "x"}) == ""
    assert server._repository_read_only_error(
        "diff_files", {"root": ".", "left": "a.py", "right": "b.py"}) == ""
    outside = str(tmp_path.resolve())
    for name, args in (
        ("find_references", {"root": outside, "symbol": "x"}),
        ("diff_files", {"root": outside, "left": "a", "right": "b"}),
        ("diff_files", {"root": ".", "left": outside, "right": "b.py"}),
        ("diff_files", {"root": ".", "left": "a.py", "right": outside}),
    ):
        error = server._repository_read_only_error(name, args)
        assert error.startswith("ERROR: repository read-only path rejected"), (name, error)


def test_a_read_only_gate_refuses_a_glob_that_reads_a_secret_file(tmp_path, monkeypatch):
    (tmp_path / "ok.py").write_text("admin_user = 1\n")
    (tmp_path / "secrets.txt").write_text("admin_user hunter22-plain\n")
    (tmp_path / "id_rsa").write_text("KEYID ZXCVBNMASDFGHJKL\n")
    from sonder_runtime.adapters.filesystem import read_only_root_gate as gate
    monkeypatch.setattr(gate.file_ops, "resolve_repository_read_path",
                        lambda path, **_kw: tmp_path)
    for glob in ("**/*", "secrets.txt", "id_rsa", "*"):
        error = server._repository_read_only_error(
            "find_references", {"root": str(tmp_path), "symbol": "admin_user", "glob": glob})
        assert error.startswith("ERROR: repository read-only path rejected"), (glob, error)
        assert "secret" in error
    assert server._repository_read_only_error(
        "find_references", {"root": str(tmp_path), "symbol": "admin_user", "glob": "ok.py"}) == ""
    assert server._repository_read_only_error(
        "find_references", {"root": str(tmp_path), "symbol": "x", "glob": "/etc/*"}
    ).startswith("ERROR: repository read-only path rejected")


def test_the_repository_help_lists_tool_search_beside_tool_help():
    text = server.REPOSITORY_AGENT_TOOL_HELP
    assert "- tool_help:" in text and "- tool_search:" in text


# --- review fixes: session race, run_code deletes, alias/find/unlink deletes --

def test_a_concurrent_global_lowering_cannot_leave_a_session_above_the_ceiling(mode_state):
    import threading
    _post(_Handler(), {"mode": "auto", "scope": "global"})
    entered, release, results = threading.Event(), threading.Event(), {}
    original = pm.set_mode

    def slow_set_mode(name):
        entered.set()
        release.wait(5)
        return original(name)

    def session_call():
        with pm.mode_scope("owner", "sess-A"):
            try:
                results["session"] = pm.set_session_mode("auto")
            except ValueError as error:
                results["session"] = error

    pm.set_mode = slow_set_mode
    try:
        worker = threading.Thread(target=session_call)
        worker.start()
        assert entered.wait(5)
        lowerer = threading.Thread(target=lambda: pm.set_global_mode("manual"))
        lowerer.start()  # must wait for the session write, then clear it
        release.set()
        worker.join(5)
        lowerer.join(5)
    finally:
        pm.set_mode = original
    assert pm.global_mode() == pm.MANUAL
    with pm.mode_scope("owner", "sess-A"):
        assert pm.current_mode() == pm.MANUAL


EXTRA_HOST_DELETES = (
    ("run_code", {"language": "bash", "code": "rm -f notes/old.txt"}),
    ("run_code", {"language": "powershell", "code": "Remove-Item notes/old.txt"}),
    ("run_code", {"language": "python", "code": "import os\nos.remove('notes/old.txt')"}),
    ("run_code", {"language": "python", "code": "from pathlib import Path\nPath('x').unlink()"}),
    ("parallel_run_code", {"jobs_json": json.dumps([
        {"language": "python", "code": "print(1)"},
        {"language": "bash", "code": "rm -f notes/old.txt"}])}),
    ("workspace_run", {"program": "git", "args_json": json.dumps(
        ["-c", "alias.zap=rm", "zap", "x"])}),
    ("workspace_run", {"program": "git", "args_json": json.dumps(
        ["-c", "alias.c=clean", "c", "-fdx"])}),
    ("workspace_run", {"program": "find", "args_json": json.dumps([".", "-delete"])}),
    ("workspace_run", {"program": "unlink", "args_json": json.dumps(["x"])}),
    ("workspace_run", {"program": "shred", "args_json": json.dumps(["-u", "x"])}),
)


@pytest.mark.parametrize("name, args", EXTRA_HOST_DELETES)
def test_route_around_deletes_always_ask_even_in_auto(shipped_rules, name, args):
    _store, lookup = shipped_rules
    assert pm.always_asks(name, args)
    decision = pm.decide(name, interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                         arguments=args, surface="http")
    assert decision.action == pm.DENY and decision.source == "unattended", decision
    asked = pm.decide(name, interactive=True, mode=pm.AUTO, rule_lookup=lookup, arguments=args)
    assert asked.action == pm.ASK, asked


def test_ordinary_code_and_find_calls_are_not_swept_into_the_delete_ask(shipped_rules):
    for name, args in (
        ("run_code", {"language": "python", "code": "print(sum(range(10)))"}),
        ("workspace_run", {"program": "find", "args_json": json.dumps([".", "-name", "x"])}),
        ("workspace_run", {"program": "git", "args_json": json.dumps(["log", "-n", "3"])}),
    ):
        assert not pm.always_asks(name, args), (name, args)


# --- review cycle 3 ----------------------------------------------------------
# Item 1: the root gate must not pass a glob or a match that leaves the root.

_REJECTED = "ERROR: repository read-only path rejected: "


def _gate_error(monkeypatch, root, glob):
    monkeypatch.setattr(read_only_root_gate.file_ops, "resolve_repository_read_path",
                        lambda path, **_kw: root)
    return server._repository_read_only_error(
        "find_references", {"root": str(root), "symbol": "x", "glob": glob})


@pytest.fixture
def escape_tree(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "a").mkdir(parents=True)
    (root / ".github").mkdir()
    (outside / "sub").mkdir(parents=True)
    (root / "ok.py").write_text("x = 1\n")
    (root / ".github" / "ci.py").write_text("x = 1\n")
    (outside / "plain.py").write_text("x = 1\n")
    (outside / ".env").write_text("K=v\n")
    (outside / "sub" / "deep.py").write_text("x = 1\n")
    return root, outside


@pytest.mark.parametrize("glob", [
    "../*", "a/../../*", "../outside/.env", "../outside/*", "a\\..\\..\\outside\\*",
    "/etc/*", "C:/Windows/*", "C:*", "\\\\srv\\share\\*", "/",
])
def test_the_root_gate_refuses_an_absolute_or_climbing_glob(monkeypatch, escape_tree, glob):
    root, _outside = escape_tree
    error = _gate_error(monkeypatch, root, glob)
    assert error == _REJECTED + "glob must be a relative pattern inside the root", (glob, error)


def _symlink_or_skip(link, target, is_dir):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except OSError as exc:  # Windows without developer mode or the privilege
        pytest.skip("os.symlink is not permitted here (%s); Linux CI runs this" % exc)


def test_the_root_gate_refuses_a_symlinked_file_that_leaves_the_root(monkeypatch, escape_tree):
    root, outside = escape_tree
    _symlink_or_skip(root / "link.py", outside / "plain.py", False)
    for glob in ("link.py", "*.py", "**/*.py"):
        error = _gate_error(monkeypatch, root, glob)
        assert error == _REJECTED + (
            "glob match link.py resolves outside the root (a link); narrow the glob"), (glob, error)
        assert str(outside) not in error


def test_the_root_gate_refuses_a_symlinked_directory_that_leaves_the_root(monkeypatch, escape_tree):
    root, outside = escape_tree
    _symlink_or_skip(root / "linkdir", outside / "sub", True)
    error = _gate_error(monkeypatch, root, "linkdir/*")
    assert error.startswith(_REJECTED + "glob match linkdir"), error
    assert "resolves outside the root (a link)" in error
    assert str(outside) not in error


def test_the_root_gate_passes_globs_inside_a_tree_with_no_escaping_link(monkeypatch, escape_tree):
    root, _outside = escape_tree
    (root / "a" / "mod.py").write_text("x = 1\n")
    for glob in ("**/*.py", ".github/*", "ok.py", "a/*.py", "*"):
        assert _gate_error(monkeypatch, root, glob) == "", glob


def test_a_relative_root_is_refused_when_the_reader_would_resolve_it_elsewhere(
        monkeypatch, tmp_path):
    monkeypatch.chdir(file_ops.workspace_root())
    assert server._repository_read_only_error(
        "find_references", {"root": ".", "symbol": "x", "glob": "ok.py"}) == ""
    monkeypatch.chdir(tmp_path)  # the reader resolves "." here, the gate in the workspace
    for name, args in (
        ("find_references", {"root": ".", "symbol": "x", "glob": "*.py"}),
        ("diff_files", {"root": ".", "left": "a.py", "right": "b.py"}),
    ):
        error = server._repository_read_only_error(name, args)
        assert error.startswith(_REJECTED + "relative root does not name the same directory"), (
            name, error)
        assert str(tmp_path) not in error


# Item 3: deletes are found in every string leaf, whatever glues the verb on.

ROUTED_DELETES = (
    ("workspace_run", {"program": "sh", "args_json": "[]", "stdin": "rm -f notes/x.txt"}),
    ("workspace_run", {"program": "python", "args_json": "[]",
                       "stdin": "import os; os.remove('notes/x.txt')"}),
    ("workspace_run", {"program": "sh", "args_json": json.dumps(["-c", "true&&rm -f x"])}),
    ("workspace_run", {"program": "sh", "args_json": json.dumps(["-c", "echo|rm -f x"])}),
    ("run_code", {"language": "bash", "code": "x=1\ntrue&&rm -f x"}),
    ("run_code", {"language": "python", "code": "print(1)", "stdin": "rm -f x"}),
    ("script_run", {"path": "del.py", "args_json": "[]", "stdin": "rm x"}),
    ("parallel_run_code", {"jobs_json": json.dumps([
        {"language": "bash", "code": "echo hi", "stdin": "true&&rm -rf notes"}])}),
    ("build_run", {"root": ".", "command": "rm -rf notes"}),
    ("build_run", {"root": ".", "command": "make clean&&del /q notes\\x"}),
)


@pytest.mark.parametrize("name, args", ROUTED_DELETES)
def test_a_delete_in_any_argument_always_asks(shipped_rules, name, args):
    _store, lookup = shipped_rules
    assert delete_calls.deletes_files(name, args), (name, args)
    assert pm.always_asks(name, args)
    decision = pm.decide(name, interactive=False, mode=pm.AUTO, rule_lookup=lookup,
                         arguments=args, surface="http")
    assert decision.action == pm.DENY and decision.source == "unattended", decision
    asked = pm.decide(name, interactive=True, mode=pm.AUTO, rule_lookup=lookup, arguments=args)
    assert asked.action == pm.ASK, asked


def test_build_run_is_a_host_program_tool():
    assert "build_run" in delete_calls.HOST_PROGRAM_TOOLS
    assert not delete_calls.deletes_files("build_run", {"root": ".", "command": "make all"})


def test_ordinary_calls_stay_out_of_the_delete_ask_with_the_leaf_scan():
    for name, args in (
        ("run_code", {"language": "python", "code": "print(sum(range(10)))", "stdin": ""}),
        ("workspace_run", {"program": "find", "args_json": json.dumps([".", "-name", "x"])}),
        ("workspace_run", {"program": "git", "args_json": json.dumps(["log", "-n", "3"])}),
        ("workspace_run", {"program": "python", "args_json": json.dumps(["-m", "pytest", "-q"]),
                           "stdin": ""}),
        ("workspace_run", {"program": "git", "args_json": "[]"}),
        ("text_search", {"query": "rm -rf"}),
    ):
        assert not delete_calls.deletes_files(name, args), (name, args)
        assert not pm.always_asks(name, args), (name, args)


def test_unreadable_host_arguments_fail_closed():
    assert delete_calls.deletes_files("workspace_run", ["rm", "x"])
    assert delete_calls.deletes_files("run_code", "print(1)")
    assert not delete_calls.deletes_files("workspace_run", None)
    assert not delete_calls.deletes_files("text_search", ["rm", "x"])
    deep = "[" * 100_000 + "]" * 100_000  # the JSON decoder gives up: still an ask
    assert delete_calls.deletes_files("workspace_run", {"stdin": deep})


def test_always_asks_fails_closed_for_host_tools_when_the_scan_raises(monkeypatch):
    def boom(_name, _args):
        raise RuntimeError("scan fault")

    monkeypatch.setattr(delete_calls, "deletes_files", boom)
    assert pm.always_asks("workspace_run", {"program": "git", "args_json": "[\"status\"]"})
    assert pm.always_asks("build_run", {"command": "make"})
    assert pm.always_asks("file_delete", {"path": "x"})
    assert not pm.always_asks("text_search", {"query": "x"})


# Item 4, as amended in review round 6: the text path takes a quote after an
# unquoted value, as 7f95afccc did, because it cannot tell a JSON string's
# closing quote from a password's last character. Parseable JSON keeps its
# quotes through the structural path instead.

def test_redaction_inside_truncated_json_takes_the_trailing_quote_as_before():
    out = redaction.redact_text('{"text": "db_password=hunter22", "x": 1')
    assert out == '{"text": "db_password=[REDACTED], "x": 1'
    assert redaction.redact_text('password=hunter22"') == "password=[REDACTED]"


def test_a_json_escaped_quoted_secret_is_redacted():
    out = redaction.redact_text('password = \\"hunter22\\"')
    assert out == "password = [REDACTED]"
    truncated = redaction.redact_text('{"t": "password = \\"hunter22\\" ok", "n": 1')
    assert truncated == '{"t": "password = [REDACTED] ok", "n": 1'
    assert truncated.count('"') % 2 == 0  # the quotes left are balanced


@pytest.mark.parametrize("text, secret", [
    ('password="hunter22', "hunter22"),
    ('password="hunter 22"', "hunter"),
    ("password=\"it's-secret\"", "secret"),
    ("password: 'hunter22'", "hunter22"),
    ("password=hunter22", "hunter22"),
    ("password=ab\\cdefgh", "cdefgh"),
    ('{"password": "hunter22"}', "hunter22"),
])
def test_text_redaction_has_no_regression_on_unbalanced_or_spaced_values(text, secret):
    assert secret not in redaction.redact_text(text)



# --- review fixes: byte-identical JSON, loop-surface delete ask ---------------

def test_unredacted_json_output_is_returned_byte_identical():
    raw = '{"a":true,"name":"café","n":[1,2]}'
    assert _run_gateway(raw) == raw
    raw_utf8 = '{"name":"café","ok":true}'
    assert _run_gateway(raw_utf8) == raw_utf8


def test_redacted_json_output_keeps_non_ascii_unescaped():
    out = _run_gateway('{"name":"café","password":"hunter22xyz"}')
    assert "café" in out and "hunter22xyz" not in out
    json.loads(out)


@pytest.mark.parametrize("name, args", [
    ("workspace_run", {"type": "workspace_run", "program": "rm",
                       "args_json": json.dumps(["-rf", "notes"])}),
    ("workspace_run", {"type": "workspace_run", "program": "git",
                       "args_json": json.dumps(["rm", "-f", "x"])}),
    ("script_run", {"type": "script_run", "language": "bash", "stdin": "rm -rf x"}),
])
def test_the_loop_surface_refuses_host_deletes_in_auto(shipped_rules, monkeypatch, name, args):
    _store, lookup = shipped_rules
    real = pm.decide

    def decide(tool, **kwargs):
        kwargs.update(mode=pm.AUTO, rule_lookup=lookup)
        return real(tool, **kwargs)

    monkeypatch.setattr(pm, "decide", decide)
    refusal = server._loop_permission_refusal(name, args)
    assert refusal is not None and refusal["ok"] is False
    assert "choose a different action" not in refusal["output"]



def test_a_call_nested_past_the_scan_depth_asks():
    args = {"program": "echo"}
    for _ in range(delete_calls.MAX_DEPTH + 2):
        args = {"x": args}
    assert delete_calls.deletes_files("workspace_run", args)


# --- review round 5: the parsed structure must not lose what the raw text holds

def _valued_redactor(*values):
    from sonder_runtime.platform.logging import Redactor
    return PatternOutputRedactor(Redactor(secret_values=values, env={}).redact)


@pytest.mark.parametrize("make", [PatternOutputRedactor, _production_redactor])
@pytest.mark.parametrize("raw, secret", [
    ('{"note":"db_password=hunter22xyz","note":"ok"}', "hunter22xyz"),
    ('{"k":"AKIAIOSFODNN7EXAMPLE","k":1}', "AKIAIOSFODNN7EXAMPLE"),
    ('{"password":"hunter22xyz","password":""}', "hunter22xyz"),
    ('{"a":{"note":"token: abcdefgh","note":"ok"},"password":"x1234567"}', "abcdefgh"),
])
def test_a_secret_hidden_behind_a_duplicate_json_key_is_redacted(raw, secret, make):
    out = _run_gateway(raw, make())
    assert secret not in out, out


@pytest.mark.parametrize("raw", [
    '{"pin": 31415926, "note": "ok"}',
    '{"pin": 31415926, "password": "abcdefgh"}',
    '{"pin": 31415926.0, "note": "ok"}',
    '{"v": 1.31415926000000000000000001}',
])
def test_a_known_secret_value_held_as_a_json_number_is_redacted(raw):
    out = _run_gateway(raw, _valued_redactor("31415926"))
    assert "31415926" not in out, out


def test_redacted_json_with_a_lone_surrogate_escape_stays_encodable():
    out = _run_gateway('{"a":"\ud800","password":"hunter22xyz"}')
    assert "hunter22xyz" not in out
    out.encode("utf-8")
    assert json.loads(out)["a"] == "\ud800"


@pytest.mark.parametrize("raw", [
    '{"a": NaN, "b": [Infinity, -Infinity], "name": "x"}',
    '{"big": 123456789012345678901234567890, "f": 1.5, "e": 1e5}',
    '  {"a": 1}\n',
])
def test_lossless_json_still_comes_back_byte_identical(raw):
    assert _run_gateway(raw) == raw


_PEM_BODY = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gunVTLw7onLRnrq0"


@pytest.mark.parametrize("make", [PatternOutputRedactor, _production_redactor])
@pytest.mark.parametrize("raw, secret", [
    # The text path sees context across JSON strings that a per-string pass
    # cannot; another redacted field must not switch that coverage off.
    ('{"password":"hunter22xyz","l":["-----BEGIN RSA PRIVATE KEY-----", "%s", '
     '"-----END RSA PRIVATE KEY-----"]}' % _PEM_BODY, _PEM_BODY[:24]),
    ('{"password":"hunter22xyz","h":"Authorization:","v":"Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
    ('["-----BEGIN RSA PRIVATE KEY-----", "%s", "-----END RSA PRIVATE KEY-----"]'
     % _PEM_BODY, _PEM_BODY[:24]),
    ('{"h":"Authorization:","v":"Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
])
def test_a_secret_the_text_path_sees_across_json_strings_is_redacted(raw, secret, make):
    out = _run_gateway(raw, make())
    assert secret not in out, out
    assert "hunter22xyz" not in out


# --- review round 6: a value with an interior quote must not leak through JSON

@pytest.mark.parametrize("make", [PatternOutputRedactor, _production_redactor])
@pytest.mark.parametrize("raw, fragments", [
    ('{"password":"hunter22xyz","note":"token=abc\\"defghijk"}', ("abc", "defghijk")),
    ('{"note":"token=abc\\"defghijk"}', ("abc", "defghijk")),
    ('[{"a":"password=abcd\\\\\\"efgh"}]', ("abcd", "efgh")),
    ('{"note":"password=p@ss\\"word123"}', ("p@ss", "word123")),
    ('{"a":"password: \\"ab,cdef\\""}', ("ab,", "cdef")),
])
def test_a_json_string_value_with_an_interior_quote_is_redacted_whole(raw, fragments, make):
    out = _run_gateway(raw, make())
    assert "hunter22xyz" not in out
    for fragment in fragments:
        assert fragment not in out, out


# The credential rule at 7f95afccc, which redacted the raw JSON text.
_BASELINE_CREDENTIAL_RULE = re.compile(
    r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
    r"passwd|credential|\b(?:pwd|(?:aws[-_])?secret[-_]access[-_]key))"
    r"[\"']?\s*[:=]\s*)"
    r"([\"']?[^\s\"',;}{]{4,}[\"']?)"
)


def _baseline_redact(module, name, redact, texts):
    """``redact`` over *texts* as 7f95afccc did: its credential rule, no second stage."""
    patterns = getattr(module, name)
    hits = [i for i, p in enumerate(patterns) if "passwd|credential" in getattr(p, "pattern", "")]
    assert len(hits) == 1, hits
    swapped = patterns[:hits[0]] + (_BASELINE_CREDENTIAL_RULE,) + patterns[hits[0] + 1:]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(module, name, swapped)
        mp.setattr(module, "_sub_credential_values", lambda text: text, raising=False)
        assert redact('password = \\"hunter22\\"') == 'password = \\"hunter22\\"'  # swap took
        return [redact(text) for text in texts]


def _note_leak(out, key):
    """What is left of the value after *key* in the ``note`` string of *out*."""
    try:
        note = json.loads(out)["note"]
    except (ValueError, KeyError, TypeError):
        note = None
    if isinstance(note, str):
        return note.split(key, 1)[1].replace("[REDACTED]", "") if key in note else ""
    tail = out[out.index(key) + len(key):].replace("[REDACTED]", "")
    tail = tail[:-1] if tail.endswith("}") else tail
    if tail.endswith('"') and (len(tail) - 1 - len(tail[:-1].rstrip("\\"))) % 2 == 0:
        tail = tail[:-1]  # the JSON string's own closing quote
    return re.sub(r"\\(.)", r"\1", tail)


@pytest.mark.parametrize("copy", ["domain", "platform"])
def test_gateway_never_leaves_more_of_a_credential_value_than_the_baseline(copy):
    from sonder_runtime.platform import logging as platform_logging
    if copy == "domain":
        module, name, make = redaction, "PATTERNS", PatternOutputRedactor
        redact = redaction.redact_text
    else:
        module, name = platform_logging, "_PATTERNS"
        redact = platform_logging.Redactor(env={}).redact

        def make():
            return PatternOutputRedactor(redact)
    rng = random.Random(20261119)
    alphabet = "abcdefgh1234@#!%\"'\\"
    cases = []
    for _ in range(300):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        cases.append((json.dumps({"note": "password=" + value}), "password="))
        cases.append((json.dumps({"password": "hunter22xyz", "note": "token=" + value}), "token="))
    baseline = _baseline_redact(module, name, redact, [raw for raw, _key in cases])
    worse = []
    for (raw, key), before in zip(cases, baseline, strict=True):
        out = _run_gateway(raw, make())
        if "hunter22xyz" in out or not _note_leak(before, key).endswith(_note_leak(out, key)):
            worse.append((raw, before, out))
    assert not worse, "%d of %d: %r" % (len(worse), len(cases), worse[:4])


# --- review round 7: a private path with a space, through the gateway --------

_SPACED_PRIVATE_DIR = "C:/Users/John Smith/.sonder"


def _path_redactors():
    from sonder_runtime.platform import logging as platform_logging
    domain = PatternOutputRedactor(
        lambda text: redaction.redact_text(text, path_prefixes=(_SPACED_PRIVATE_DIR,)))
    platform = PatternOutputRedactor(
        platform_logging.Redactor(env={}, path_prefixes=(_SPACED_PRIVATE_DIR,)).redact)
    return {"domain": domain, "platform": platform}


@pytest.mark.parametrize("copy", ["domain", "platform"])
@pytest.mark.parametrize("raw", [
    '{"note":"token=\'x\'C:/Users/John Smith/.sonder/secrets.env"}',
    "token='x'C:/Users/John Smith/.sonder/secrets.env",
    '["password=ab\\"cC:/Users/John Smith/.sonder/a"]',
    '{"note":"password=\\"ab,c\\"C:/Users/John Smith/.sonder/secrets.env tail"}',
])
def test_gateway_never_shows_part_of_a_spaced_private_path(raw, copy):
    out = _run_gateway(raw, _path_redactors()[copy])
    for fragment in ("Smith", "John", ".sonder", "Users"):
        assert fragment not in out, out


# --- review round 7: delete commands the word scan missed --------------------

@pytest.mark.parametrize("args", [
    {"program": "cmd", "args_json": json.dumps(["/c", r"node_modules\.bin\rimraf.cmd dist"])},
    {"stdin": "rimraf.CMD dist"},
    {"stdin": "del.bat x"},
    {"stdin": "rm.ps1 x"},
    {"stdin": r"C:\tools\rm.com x"},
    {"stdin": "RM.EXE x"},
    {"stdin": "rimraf.cmd.exe dist"},
    {"stdin": "cscript //nologo del.VBS x"},
    {"stdin": "wscript rm.js x"},
])
def test_a_delete_verb_with_a_pathext_suffix_asks(args):
    assert delete_calls.deletes_files("workspace_run", args), args


# Base64 of UTF-16LE "Remove-Item x".
_ENCODED_REMOVE = "UgBlAG0AbwB2AGUALQBJAHQAZQBtACAAeAA="


@pytest.mark.parametrize("args", [
    {"program": "pwsh", "args_json": json.dumps(["-e", _ENCODED_REMOVE])},
    {"program": "powershell", "args_json": json.dumps(["-EncodedCommand", _ENCODED_REMOVE])},
    {"stdin": "powershell.exe -enc " + _ENCODED_REMOVE},
    {"stdin": "pwsh -ec " + _ENCODED_REMOVE},
    {"stdin": "pwsh -En " + _ENCODED_REMOVE},
    {"stdin": "powershell -encodedc " + _ENCODED_REMOVE},
    {"stdin": "powershell /enc " + _ENCODED_REMOVE},
    {"stdin": "pwsh --encodedcommand " + _ENCODED_REMOVE},
    {"stdin": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE -E " + _ENCODED_REMOVE},
])
def test_an_encoded_powershell_command_asks(args):
    assert delete_calls.deletes_files("workspace_run", args), args


@pytest.mark.parametrize("args", [
    {"program": "cmd", "args_json": json.dumps(["/c", "de^l x"])},
    {"stdin": 'cmd /c "de^l x"'},
    {"stdin": "cmd.exe /c r^m x"},
    {"program": "cmd.exe", "args_json": json.dumps(["/c", "e^rase x"])},
])
def test_a_caret_escaped_cmd_word_asks(args):
    assert delete_calls.deletes_files("workspace_run", args), args


def test_the_new_delete_rules_leave_ordinary_calls_alone():
    for args in (
        {"program": "pwsh", "args_json": json.dumps(["-NoProfile", "-Command", "Get-ChildItem"])},
        {"program": "powershell", "args_json": json.dumps(["-ExecutionPolicy", "RemoteSigned", "-File", "b.ps1"])},
        {"program": "cmd", "args_json": json.dumps(["/c", "dir"])},
        {"program": "python", "args_json": "[]", "stdin": "import re; re.match('^a', 'a')"},
        {"stdin": "start example.com"},
    ):
        assert not delete_calls.deletes_files("workspace_run", args), args
