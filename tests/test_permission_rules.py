import permission_rules


def test_permission_rules_default_match(tmp_path):
    rule = permission_rules.check(tmp_path, "file_delete")
    assert rule["action"] == "deny"


def test_permission_rules_add_rule_takes_precedence(tmp_path):
    permission_rules.add_rule(tmp_path, "file_delete", "ask", "manual approval")
    rule = permission_rules.check(tmp_path, "file_delete")
    assert rule["action"] == "ask"
    assert rule["note"] == "manual approval"


def test_permission_policy_formats_single_tool(tmp_path):
    out = permission_rules.format_policy(tmp_path, "web_search")
    assert "permission check: web_search" in out
    assert "action: ask" in out
