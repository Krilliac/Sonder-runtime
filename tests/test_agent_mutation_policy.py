from sonder_runtime.domain import agent_mutation_policy


def test_invocation_mutation_policy_classifies_defaults_and_opt_ins():
    assert agent_mutation_policy.invocation_mutates("file_write", {})
    assert not agent_mutation_policy.invocation_mutates("json_patch", {})
    assert agent_mutation_policy.invocation_mutates("json_patch", {"mode": "apply"})
    assert not agent_mutation_policy.invocation_mutates("rename_symbol", {"dry_run": True})
    assert agent_mutation_policy.invocation_mutates("rename_symbol", {"dry_run": False})
    assert agent_mutation_policy.invocation_mutates("apply_patch", {})
    assert not agent_mutation_policy.invocation_mutates("apply_patch", {"check_only": True})
    assert not agent_mutation_policy.invocation_mutates("lint_run", {"fix": False})
    assert agent_mutation_policy.invocation_mutates("lint_run", {"fix": True})
    assert not agent_mutation_policy.invocation_mutates("format_code", {"check_only": True})
    assert agent_mutation_policy.invocation_mutates("format_code", {})


def test_invocation_mutation_policy_handles_unknown_and_non_dict_args():
    assert not agent_mutation_policy.invocation_mutates("status", {})
    assert agent_mutation_policy.invocation_mutates("file_write", None)


def test_server_keeps_identity_compatible_policy_aliases():
    import server

    assert server._WORK_MUTATION_TOOLS is agent_mutation_policy.WORK_MUTATION_TOOLS
    assert server._agent_tool_mutates is agent_mutation_policy.invocation_mutates
