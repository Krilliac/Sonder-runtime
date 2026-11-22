"""WP1 ratchet for the one remaining legacy root-server dependency."""
from __future__ import annotations

import ast
import importlib.util
import textwrap
from pathlib import Path, PureWindowsPath

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"
LEGACY_ROOT = PACKAGE_ROOT / "bootstrap" / "legacy_root.py"

# Every remaining caller is explicitly excluded from this narrow migration
# slice. Keep the classification in the ratchet so a new caller cannot hide
# behind the transitional boundary.
PROHIBITED_CALLERS = {
    "sonder_runtime/bootstrap/legacy_interfaces.py": "HTTP/REPL composition",
    "sonder_runtime/bootstrap/legacy_model.py": "model bootstrap",
    "sonder_runtime/bootstrap/legacy_mcp.py": "MCP bootstrap",
}


def _imports(path: Path, *, include_relative: bool = True) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and (include_relative or node.level == 0):
            result.add(node.module)
    return result


def test_server_root_import_isolated_to_legacy_root_boundary():
    """No packaged production module may acquire ``server`` accidentally."""
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == LEGACY_ROOT:
            continue
        if "server" in _imports(path, include_relative=False):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []
    assert "server" in _imports(LEGACY_ROOT, include_relative=False)


def test_legacy_root_is_only_reached_by_bootstrap_composition_modules():
    """The exception remains behind explicit startup seams, not app code."""
    expected = set(PROHIBITED_CALLERS)
    callers = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "sonder_runtime.bootstrap.legacy_root" in _imports(path) or (
            path.parent == LEGACY_ROOT.parent
            and "legacy_root" in _imports(path)
        ):
            callers.add(path.relative_to(REPO_ROOT).as_posix())

    assert callers == expected


def test_no_remaining_root_caller_is_in_the_safe_migration_scope():
    """All remaining callers are explicitly excluded seams."""
    callers = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == LEGACY_ROOT:
            continue
        imports = _imports(path)
        if "sonder_runtime.bootstrap.legacy_root" in imports or (
            path.parent == LEGACY_ROOT.parent and "legacy_root" in imports
        ):
            callers.add(path.relative_to(REPO_ROOT).as_posix())

    assert callers == set(PROHIBITED_CALLERS)
    assert all(PROHIBITED_CALLERS[path] for path in callers)


# ---- dynamic reach of the root server module ---------------------------------

def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_architecture_under_test", REPO_ROOT / "scripts" / "check_architecture.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


# The six sites that may reach ``server`` dynamically, pinned here
# independently of the gate's own constant so editing one cannot hide in the other.
EXPECTED_DYNAMIC_REACHES = {
    ("sonder_runtime/adapters/command_catalog.py", "console_tools"): 1,
    ("sonder_runtime/adapters/command_catalog.py", "_http_slash_chain"): 1,
    ("sonder_runtime/adapters/command_catalog.py", "catalog"): 1,
    ("sonder_runtime/adapters/model_tool_grants.py", "host_classifier"): 1,
    ("sonder_runtime/adapters/tool_lane_guard.py", "default_route_refusal"): 1,
    ("sonder_runtime/bootstrap/legacy_root.py", "detach_owned_application"): 1,
}

CATALOG = Path("sonder_runtime") / "adapters" / "command_catalog.py"
GUARD = Path("sonder_runtime") / "adapters" / "tool_lane_guard.py"
GRANTS = Path("sonder_runtime") / "adapters" / "model_tool_grants.py"

_CATALOG_SOURCE = '''
import importlib

def console_tools():
    server = importlib.import_module("server")
    return server

def _http_slash_chain():
    server = importlib.import_module("server")
    return server

def catalog():
    server = importlib.import_module("server")
    return server
'''
_GUARD_SOURCE = '''
import sys

def default_route_refusal():
    return sys.modules.get("server")
'''
_GRANTS_SOURCE = '''
import sys

def host_classifier():
    return sys.modules.get("server")
'''
_ROOT_SOURCE = '''
import sys

def detach_owned_application(application):
    return sys.modules.get("server")
'''
_ROOT = Path("sonder_runtime") / "bootstrap" / "legacy_root.py"


def _trees(**overrides):
    sources = {
        CATALOG: _CATALOG_SOURCE, GUARD: _GUARD_SOURCE, GRANTS: _GRANTS_SOURCE, _ROOT: _ROOT_SOURCE,
    }
    sources.update({Path(key): value for key, value in overrides.items()})
    return {path: ast.parse(textwrap.dedent(text)) for path, text in sources.items()}


def _package_trees():
    return {
        path.relative_to(REPO_ROOT): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in PACKAGE_ROOT.rglob("*.py")
    }


def test_the_gate_pins_exactly_the_six_current_sites(gate):
    assert gate.DYNAMIC_SERVER_REACH_ALLOWLIST == EXPECTED_DYNAMIC_REACHES


def test_the_current_tree_passes_the_dynamic_reach_gate_and_sees_its_six_sites(gate):
    trees = _package_trees()
    assert gate.dynamic_server_reach_violations(trees) == []
    # Not vacuous: the scan really finds the six reaches in the real tree.
    seen = {}
    for path, tree in trees.items():
        for function in gate.dynamic_server_reaches(tree):
            seen[(path.as_posix(), function)] = seen.get((path.as_posix(), function), 0) + 1
    assert seen == EXPECTED_DYNAMIC_REACHES


def test_the_synthetic_six_sites_pass_the_real_allowlist(gate):
    assert gate.dynamic_server_reach_violations(_trees()) == []


@pytest.mark.parametrize("spelling", [
    'import importlib\nimportlib.import_module("server")',
    'from importlib import import_module as load\nload("server")',
    'import importlib\nimportlib.import_module(name="server")',
    '__import__("server")',
    'import sys\nsys.modules["server"]',
    'import sys\nsys.modules.get("server")',
    'import sys\nsys.modules.pop("server", None)',
    'import sys as system\nsystem.modules["server"]',
    'from sys import modules\nmodules["server"]',
    'from sys import modules as loaded\nloaded.get("server")',
])
def test_every_dynamic_reach_spelling_fails_in_a_new_file(gate, spelling):
    new = Path("sonder_runtime") / "application" / "sneaky.py"
    trees = {new: ast.parse(spelling)}
    problems = gate.dynamic_server_reach_violations(trees, allowlist={})
    assert problems and "sonder_runtime/application/sneaky.py" in problems[0]
    assert "<module>" in problems[0]


@pytest.mark.parametrize("harmless", [
    'import importlib\nimportlib.import_module("json")',
    'import sys\nsys.modules["json"]',
    'import sys\nsys.modules.get("server_other")',
    'modules = {}\nmodules["server"]',
    'registry = {}\nregistry.get("server")',
    '"server"',
])
def test_spellings_that_do_not_reach_server_are_not_flagged(gate, harmless):
    new = Path("sonder_runtime") / "application" / "fine.py"
    assert gate.dynamic_server_reach_violations({new: ast.parse(harmless)}, allowlist={}) == []


def test_a_reach_is_attributed_to_its_enclosing_function(gate):
    source = textwrap.dedent('''
        import sys
        class Box:
            def method(self):
                def inner():
                    return sys.modules.get("server")
                return inner
        async def later():
            return __import__("server")
    ''')
    assert gate.dynamic_server_reaches(ast.parse(source)) == ["Box.method.inner", "later"]


def test_a_second_import_module_in_console_tools_fails(gate):
    doubled = _CATALOG_SOURCE.replace(
        'def console_tools():\n    server = importlib.import_module("server")',
        'def console_tools():\n    server = importlib.import_module("server")\n'
        '    again = importlib.import_module("server")',
    )
    assert doubled != _CATALOG_SOURCE
    problems = gate.dynamic_server_reach_violations(_trees(**{str(CATALOG): doubled}))
    assert len(problems) == 1
    assert "command_catalog.py: console_tools: 2 dynamic reach(es)" in problems[0]
    assert "pins 1" in problems[0]


def test_a_new_reach_from_another_function_of_tool_lane_guard_fails(gate):
    widened = _GUARD_SOURCE + '''
def other_helper():
    import sys
    return sys.modules["server"]
'''
    problems = gate.dynamic_server_reach_violations(_trees(**{str(GUARD): widened}))
    assert len(problems) == 1
    assert "tool_lane_guard.py: other_helper:" in problems[0]
    assert "allowlist pins 0" in problems[0]


def test_a_new_function_in_command_catalog_fails_even_though_the_file_is_allowlisted(gate):
    widened = _CATALOG_SOURCE + '''
def brand_new():
    return importlib.import_module("server")
'''
    problems = gate.dynamic_server_reach_violations(_trees(**{str(CATALOG): widened}))
    assert len(problems) == 1 and "brand_new" in problems[0]


def test_a_removed_reach_leaves_a_stale_pin_that_must_shrink(gate):
    trimmed = _GUARD_SOURCE.replace('sys.modules.get("server")', "None")
    problems = gate.dynamic_server_reach_violations(_trees(**{str(GUARD): trimmed}))
    assert len(problems) == 1
    assert "default_route_refusal" in problems[0] and "shrink the pin" in problems[0]


def test_paths_are_compared_by_their_posix_spelling(gate):
    windows = PureWindowsPath("sonder_runtime\\adapters\\tool_lane_guard.py")
    trees = {
        windows: ast.parse(textwrap.dedent(_GUARD_SOURCE)),
        CATALOG: ast.parse(textwrap.dedent(_CATALOG_SOURCE)),
        GRANTS: ast.parse(textwrap.dedent(_GRANTS_SOURCE)),
        _ROOT: ast.parse(textwrap.dedent(_ROOT_SOURCE)),
    }
    assert gate.dynamic_server_reach_violations(trees) == []
    # A string spelling is accepted too, and the posix form is what is reported.
    trees = {"sonder_runtime/application/x.py": ast.parse('__import__("server")')}
    assert "sonder_runtime/application/x.py" in gate.dynamic_server_reach_violations(trees, allowlist={})[0]

