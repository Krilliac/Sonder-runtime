from sonder_runtime.domain import code_gate_policy


def _extract(reply):
    if "```python" not in reply:
        return None
    return {"language": "python", "code": reply.split("```python", 1)[1].split("```", 1)[0]}


def test_code_gate_policy_selects_definition_import_and_class_blocks():
    assert code_gate_policy.code_gate_target("```python\ndef run():\n    pass\n```", _extract)
    assert code_gate_policy.code_gate_target("```python\nimport json\n```", _extract)
    assert code_gate_policy.code_gate_target("```python\nclass Runner:\n    pass\n```", _extract)


def test_code_gate_policy_rejects_non_python_trivial_and_interactive_replies():
    assert code_gate_policy.code_gate_target("plain text", _extract) is None
    assert code_gate_policy.code_gate_target("```python\nprint(1)\n```", _extract) is None
    assert code_gate_policy.code_gate_target(
        "```python\ndef ask():\n    return input('name')\n```", _extract,
    ) is None


def test_server_keeps_compatibility_delegate_for_code_gate_target():
    import server

    assert server._code_gate_target("```python\ndef run():\n    pass\n```") is not None
