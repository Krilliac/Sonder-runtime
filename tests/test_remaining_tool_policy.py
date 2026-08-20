from sonder_runtime.application.tools.resource_policy import (
    Decision, PolicyRule, ResourcePolicy, ResourceRequest, StartupAuthoritySnapshot,
)


def request(**changes):
    values = {"request_id": "req-1", "tool": "file_read", "operation": "read",
              "path": r"C:\workspace\src\main.py", "workspace": r"C:\workspace",
              "origin": "mcp", "side_effect_class": "read", "persistence": "none",
              "secret_exposure": "none"}
    values.update(changes)
    return ResourceRequest(**values)


def test_resource_rule_matches_path_boundary_and_host_subdomain_boundary():
    policy = ResourcePolicy([
        PolicyRule("read-src", Decision.ALLOW, tool="file_read", path=r"C:\workspace\src/**", host="*.example.test"),
    ])
    assert policy.evaluate(request(host="api.example.test")).allowed
    assert not policy.evaluate(request(path=r"C:\workspace\src2\main.py", host="api.example.test")).allowed
    assert not policy.evaluate(request(host="example.test")).allowed


def test_all_resource_dimensions_must_match():
    policy = ResourcePolicy([PolicyRule(
        "attended-write", Decision.ATTENDED_ONLY, tool="file_write", operation="write",
        path=r"C:\workspace/**", agent_preset="editor", workspace=r"C:\workspace",
        origin="repl", side_effect_class="write", persistence="session", secret_exposure="none",
    )])
    result = policy.evaluate(request(tool="file_write", operation="write", path=r"C:\workspace\a.txt",
                                      agent_preset="editor", origin="repl", side_effect_class="write",
                                      persistence="session", attended=False))
    assert result.decision is Decision.ATTENDED_ONLY
    assert not result.allowed and result.approval_required
    assert not policy.evaluate(request(tool="file_write", operation="write", path=r"C:\workspace\a.txt",
                                        agent_preset="reviewer", origin="repl", side_effect_class="write",
                                        persistence="session", attended=True)).allowed


def test_allow_ask_deny_truth_and_receipt_are_explicit():
    policy = ResourcePolicy([
        PolicyRule("ask", Decision.ASK, tool="web_fetch", host="api.example.test"),
    ])
    ask = policy.evaluate(request(tool="web_fetch", host="api.example.test"))
    deny = policy.evaluate(request(tool="shell", operation="run"))
    assert ask.decision is Decision.ASK and not ask.allowed and ask.approval_required
    assert deny.decision is Decision.DENY and not deny.allowed and not deny.approval_required
    assert ask.receipt.request_id == "req-1" and ask.receipt.matched_rule_id == "ask"
    assert ask.receipt.authority_digest


def test_startup_authorities_are_independent_immutable_and_fail_closed():
    authorities = StartupAuthoritySnapshot.capture(unrestricted_tools=True, unrestricted_selfmod=False,
                                                    captured_at="2026-08-20T00:00:00Z")
    tools = ResourcePolicy([PolicyRule("tools", Decision.ALLOW, tool="shell", required_authority="unrestricted_tools")], authorities=authorities)
    selfmod = ResourcePolicy([PolicyRule("selfmod", Decision.ALLOW, tool="selfmod", required_authority="unrestricted_selfmod")], authorities=authorities)
    assert tools.evaluate(request(tool="shell")).allowed
    assert not selfmod.evaluate(request(tool="selfmod")).allowed
    assert authorities.permits("unrestricted_tools") and not authorities.permits("unrestricted_selfmod")
    assert authorities.digest == tools.evaluate(request(tool="shell")).receipt.authority_digest
    try:
        authorities.unrestricted_tools = False
    except AttributeError:
        pass
    else:
        raise AssertionError("startup authority snapshot must be immutable")
