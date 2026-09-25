"""Conservative literal-case projection from selfmod held-out suites.

Only syntactically isolated literal assertions qualify. Suites with pytest
configuration, fixtures, or setup hooks are left unevaluated. This projection
is not an independent or complete scorer. Candidate code runs in the host's
candidate supervisor (Windows low integrity, or the Linux uid-separated
supervisor when configured) and receives inputs, never expected values.
"""

from __future__ import annotations

import ast
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

MAX_CASES = 16
MAX_PAYLOAD_BYTES = 16_384
RESULT_PREFIX = "SELFMOD HOST CHALLENGE RESULT "


def _literal(node):
    try:
        value = ast.literal_eval(node)
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError, MemoryError):
        return None, False
    return value, True


def _direct_assertion_case(statement, module_aliases, function_aliases, function):
    """Return one call/equality case, refusing every contextual expression."""
    if (not isinstance(statement, ast.Assert)
            or not isinstance(statement.test, ast.Compare)
            or len(statement.test.ops) != 1
            or not isinstance(statement.test.ops[0], ast.Eq)
            or len(statement.test.comparators) != 1):
        return None
    call = statement.test.left
    if not isinstance(call, ast.Call):
        return None
    target = call.func
    if isinstance(target, ast.Name):
        recognized = target.id in function_aliases
    else:
        recognized = (
            isinstance(target, ast.Attribute) and target.attr == function
            and isinstance(target.value, ast.Name)
            and target.value.id in module_aliases
        )
    if not recognized or any(item.arg is None for item in call.keywords):
        return None
    args = []
    kwargs = {}
    for arg in call.args:
        value, valid = _literal(arg)
        if not valid:
            return None
        args.append(value)
    for item in call.keywords:
        value, valid = _literal(item.value)
        if not valid:
            return None
        kwargs[item.arg] = value
    expected, valid = _literal(statement.test.comparators[0])
    if not valid:
        return None
    return {"args": args, "kwargs": kwargs, "expected": expected}


def _has_applicable_conftest(path: Path) -> bool:
    """Pytest configuration can add fixtures/hooks invisible in a test AST."""
    return any((parent / "conftest.py").is_file() for parent in path.parents)


def _module_is_setup_free(tree: ast.Module) -> bool:
    """Require only imports and test functions at module scope.

    Constants, helpers, hooks, fixtures, pytest marks, and other module
    statements may change what an assertion means, so they disable projection
    for the entire file.
    """
    for index, node in enumerate(tree.body):
        if (index == 0 and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            continue
        return False
    return True


def extract_cases(paths: Sequence[Path], module: str, function: str) -> tuple[dict, ...]:
    """Project direct literal test cases without importing or executing tests."""
    if not module or not function.isidentifier():
        return ()
    cases = []
    seen = set()
    for path in paths:
        try:
            path = Path(path)
            if _has_applicable_conftest(path):
                return ()
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            return ()
        if not _module_is_setup_free(tree):
            return ()
        module_aliases = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body if isinstance(node, ast.Import)
            for alias in node.names if alias.name == module
        }
        function_aliases = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == module
            for alias in node.names if alias.name == function
        }
        for test in tree.body:
            if (not isinstance(test, ast.FunctionDef) or not test.name.startswith("test_")
                    or test.decorator_list
                    or test.args.posonlyargs or test.args.args or test.args.kwonlyargs
                    or test.args.vararg is not None or test.args.kwarg is not None):
                continue
            statements = list(test.body)
            if statements and isinstance(statements[0], ast.Expr):
                docstring = statements[0].value
                if isinstance(docstring, ast.Constant) and isinstance(docstring.value, str):
                    statements.pop(0)
            # Only take a contiguous prefix of direct assertions. Once the
            # test performs setup, control flow, or another contextual action,
            # later assertions may depend on that state and are not projected.
            test_cases = []
            for statement in statements:
                case = _direct_assertion_case(
                    statement, module_aliases, function_aliases, function
                )
                if case is None:
                    break
                test_cases.append(case)
            if not test_cases:
                continue
            for case in test_cases:
                encoded = json.dumps(case, sort_keys=True, allow_nan=False)
                if encoded not in seen:
                    cases.append(case)
                    seen.add(encoded)
                if len(cases) >= MAX_CASES:
                    return tuple(cases)
    if len(json.dumps(cases, allow_nan=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return ()
    return tuple(cases)


# The candidate process only receives inputs. Its stdout is diagnostic data;
# the parent parses a single bounded result frame and owns the final score.
_PROBE = r'''
import importlib
import json
import pathlib
import sys
import tempfile
import uuid

payload = json.loads(sys.argv[1])
root = pathlib.Path(payload["root"]).resolve()
# Import candidate source through a fresh, empty cache namespace. -B alone
# stops writes but Python can still *read* an old same-size, same-second .pyc.
sys.pycache_prefix = str(pathlib.Path(tempfile.gettempdir()) / ("sonder-host-" + uuid.uuid4().hex))
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
module = importlib.import_module(payload["module"])
origin = pathlib.Path(module.__file__).resolve()
if root not in origin.parents:
    raise SystemExit(2)
function = getattr(module, payload["function"])
values = []
for case in payload["cases"]:
    values.append(function(*case["args"], **case["kwargs"]))
print("SELFMOD HOST CHALLENGE RESULT " + json.dumps({
    "nonce": payload["nonce"], "values": values,
}, sort_keys=True, allow_nan=False))
'''


def challenge(workspace: Path, module: str, function: str, cases: Sequence[dict], *, python: str | None = None):
    """Return low-child argv and parent-private challenge metadata."""
    nonce = secrets.token_hex(16)
    inputs = [{"args": item["args"], "kwargs": item["kwargs"]} for item in cases]
    payload = {"root": str(Path(workspace).resolve()), "module": module,
               "function": function, "cases": inputs, "nonce": nonce}
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    if not cases or len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("host challenge requires bounded independent assertions")
    command = [python or sys.executable, "-I", "-c", _PROBE, encoded]
    return command, nonce


def grade(output: str, nonce: str, cases: Sequence[dict]) -> tuple[bool, str]:
    """Score in the parent; no candidate exit status or pytest summary is a grade."""
    frames = [line[len(RESULT_PREFIX):] for line in str(output).splitlines()
              if line.startswith(RESULT_PREFIX)]
    if len(frames) != 1 or len(frames[0]) > MAX_PAYLOAD_BYTES:
        return False, "host challenge has no unique bounded result"
    try:
        report = json.loads(frames[0])
        if (not isinstance(report, dict) or set(report) != {"nonce", "values"}
                or report["nonce"] != nonce or not isinstance(report["values"], list)
                or len(report["values"]) != len(cases)):
            return False, "host challenge result shape differs from trusted plan"
        observed = json.dumps(report["values"], sort_keys=True, allow_nan=False)
        expected = json.dumps([item["expected"] for item in cases], sort_keys=True,
                              allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False, "host challenge result cannot be decoded"
    if observed != expected:
        return False, "candidate outputs differ from parent-held assertions"
    receipt = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return True, f"parent-scored {len(cases)} assertion(s); expected sha256={receipt}"


def clean_replay(
    repository: Path, workspace: Path, state_root: Path, starting_commit: str,
    tested_files: dict[str, str | None], module: str, function: str,
    cases: Sequence[dict], timeout: int, protected_paths: Sequence[Path] = (),
    *, python: str | None = None,
) -> tuple[bool, str]:
    """Repeat a challenge from a fresh base worktree plus only tested files.

    The checkout and expected answers live in the supervisor-owned parent.
    Candidate code executes exclusively in the selected candidate supervisor,
    including on replay, and only that supervisor's own attestation counts;
    a missing OS boundary is a failed grade.
    """
    if not starting_commit or not tested_files:
        return False, "clean replay has no bound base commit and candidate files"
    from scripts.selfmod_linux_isolation import candidate_supervisor

    run_isolated, attestation = candidate_supervisor()

    repository = Path(repository).resolve()
    workspace = Path(workspace).resolve()
    state_root = Path(state_root).resolve()
    if state_root == workspace or workspace in state_root.parents:
        return False, "clean replay workspace overlaps candidate authority"
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sonder-clean-", dir=state_root) as parent:
        clean = Path(parent) / "candidate"
        added = False
        result = (False, "clean replay was not evaluated")
        try:
            created = subprocess.run(
                ["git", "worktree", "add", "--detach", str(clean), starting_commit],
                cwd=repository, capture_output=True, text=True, timeout=120, check=False,
            )
            if created.returncode:
                raise RuntimeError("fresh Git checkout unavailable")
            added = True
            for rel, digest in tested_files.items():
                source = workspace / rel
                destination = clean / rel
                if not destination.resolve().is_relative_to(clean.resolve()) or destination.is_symlink():
                    raise RuntimeError("clean checkout target escapes workspace")
                if digest is None:
                    destination.unlink(missing_ok=True)
                    continue
                if (source.is_symlink() or not source.resolve().is_relative_to(workspace)
                        or not source.is_file()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
                    raise RuntimeError("candidate bytes differ from tested binding")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("fresh checkout bytes differ from tested binding")

            command, nonce = challenge(clean, module, function, cases, python=python)
            isolated = run_isolated(
                command, cwd=clean, timeout=timeout,
                protected_paths=(*protected_paths, *(clean / rel for rel in tested_files)),
            )
            if (isolated.get("exit_code") != 0 or isolated.get("passed") is not True
                    or isolated.get("integrity_failed")
                    or (isolated.get("job") or {}).get("integrity") != attestation):
                result = (False, "fresh checkout lacked a successful isolated probe")
            else:
                result = grade(str(isolated.get("output") or ""), nonce, cases)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            result = (False, f"fresh checkout could not be verified ({type(exc).__name__})")
        finally:
            if added:
                try:
                    removed = subprocess.run(
                        ["git", "worktree", "remove", "--force", str(clean)],
                        cwd=repository, capture_output=True, text=True,
                        timeout=120, check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    removed = None
                if removed is None or removed.returncode:
                    result = (False, "fresh checkout cleanup failed")
        return result
