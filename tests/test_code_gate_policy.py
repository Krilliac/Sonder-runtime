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


def test_compile_only_target_and_static_checker_catch_interactive_errors():
    interactive_broken = (
        "```python\n"
        "def start_game():\n"
        "    choice = input('go? ')\n"
        "    if choice == 'yes':\n"
        "        missing_room()\n"
        "start_game()\n"
        "```"
    )
    assert code_gate_policy.code_gate_target(interactive_broken, _extract) is None
    code = code_gate_policy.code_gate_compile_only_target(interactive_broken, _extract)
    assert code is not None
    err = code_gate_policy.check_python_static_errors(code)
    assert err is not None and "missing_room" in err

    syntax_broken = "def f():\n    x = input('?'\n"
    syn_err = code_gate_policy.check_python_static_errors(syntax_broken)
    assert syn_err is not None and "SyntaxError" in syn_err

    interactive_clean = (
        "def enter_room():\n"
        "    print('entered')\n"
        "def start_game():\n"
        "    if input('go? ') == 'yes':\n"
        "        enter_room()\n"
    )
    assert code_gate_policy.check_python_static_errors(interactive_clean) is None


def test_code_gate_policy_ast_interactive_stdin_detection():
    # Comments, strings, and method calls on arbitrary objects must NOT trigger interactive skip.
    non_interactive_cases = [
        "```python\n# no input() needed\ndef f():\n    return 42\n```",
        "```python\ndef f():\n    return 'call input() later'\n```",
        "```python\nclass Reader:\n    def input(self):\n        return 1\nReader().input()\n```",
    ]
    for reply in non_interactive_cases:
        assert code_gate_policy.code_gate_target(reply, _extract) is not None, reply
        assert code_gate_policy.code_gate_compile_only_target(reply, _extract) is None, reply

    # Real interactive stdin calls (input, getpass.getpass, sys.stdin.read*) MUST be detected.
    interactive_cases = [
        "```python\ndef ask():\n    return input('? ')\n```",
        "```python\nimport builtins as b\ndef ask():\n    return b.input('? ')\n```",
        "```python\nimport getpass\ndef ask():\n    return getpass.getpass('pw: ')\n```",
        "```python\nfrom getpass import getpass as gp\ndef ask():\n    return gp('pw: ')\n```",
        "```python\nimport sys\ndef ask():\n    return sys.stdin.readline()\n```",
        "```python\nfrom sys import stdin\ndef ask():\n    return stdin.read()\n```",
    ]
    for reply in interactive_cases:
        assert code_gate_policy.code_gate_target(reply, _extract) is None, reply
        assert code_gate_policy.code_gate_compile_only_target(reply, _extract) is not None, reply


def test_check_python_static_errors_handles_recursion_and_match_case():
    # Deeply nested expressions must return a compile error rather than raising RecursionError.
    deep_expr = "x = " + ("(" * 2000) + "1" + (")" * 2000)
    err = code_gate_policy.check_python_static_errors(deep_expr)
    assert err is not None and "compile failed" in err

    # Python 3.10+ match/case bindings (MatchAs, MatchStar, MatchMapping) must have zero false positives.
    match_code = (
        "def dispatch(event):\n"
        "    match event:\n"
        "        case [fn, *rest]:\n"
        "            return fn(rest)\n"
        "        case {'handler': handler, **extra}:\n"
        "            return handler(extra)\n"
        "        case callable_obj as target:\n"
        "            return target()\n"
    )
    assert code_gate_policy.check_python_static_errors(match_code) is None

    # Deleting a name must not count as defining it.
    del_code = "def f():\n    del ghost_fn\n    ghost_fn()\n"
    del_err = code_gate_policy.check_python_static_errors(del_code)
    assert del_err is not None and "ghost_fn" in del_err
