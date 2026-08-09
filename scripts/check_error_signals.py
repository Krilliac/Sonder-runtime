"""Shrink-only inventory for legacy stringly ``ERROR:`` signaling.

This checker intentionally does not infer general error semantics. It tracks
only two statically exact categories: return expressions with a literal-derived
``ERROR:`` prefix, and calls to ``.startswith("ERROR:")``. The checked-in
baseline is an upper-bound universe: findings may disappear, but a new scope,
category, or additional occurrence fails CI.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).with_name("error_signal_baseline.json")
CATEGORY_RETURN = "return_literal_prefix"
CATEGORY_STARTSWITH = "startswith_parser"


@dataclass(frozen=True)
class Finding:
    path: str
    scope: str
    category: str
    line: int
    expression: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        normalized = _stable_ast_dump(ast.parse(self.expression, mode="eval"))
        signal = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.path, self.scope, self.category, signal


def _stable_ast_dump(value) -> str:
    """Serialize AST fields explicitly, independent of ``ast.dump`` defaults."""
    if isinstance(value, ast.AST):
        present = (
            (name, getattr(value, name, None))
            for name in value._fields
            if getattr(value, name, None) is not None
        )
        fields = ", ".join(
            "%s=%s" % (name, _stable_ast_dump(field_value))
            for name, field_value in present
        )
        return "%s(%s)" % (type(value).__name__, fields)
    if isinstance(value, list):
        return "[%s]" % ", ".join(_stable_ast_dump(item) for item in value)
    return repr(value)


def _literal_error_prefix(node: ast.AST | None) -> bool:
    static_string = _bounded_static_string(node) if node is not None else None
    if static_string is not None:
        return static_string.startswith("ERROR:")
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.startswith("ERROR:")
    if isinstance(node, ast.JoinedStr):
        return bool(node.values) and _literal_error_prefix(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _literal_error_prefix(node.left)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "format_map"}
    ):
        return _literal_error_prefix(node.func.value)
    if isinstance(node, ast.IfExp):
        return _literal_error_prefix(node.body) or _literal_error_prefix(node.orelse)
    return False


def _exact_error_startswith(node: ast.Call) -> bool:
    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        and node.args
    ):
        return False
    # Cover both ``value.startswith(prefix)`` and the equivalent unbound
    # ``str.startswith(value, prefix)`` spelling. The latter otherwise gives
    # a same-semantics escape because its first positional argument is the
    # value being inspected rather than the prefix.
    unbound = (
        isinstance(node.func.value, ast.Name)
        and node.func.value.id == "str"
    )
    prefix_index = 1 if unbound else 0
    if len(node.args) <= prefix_index:
        return False
    return _contains_error_prefix(node.args[prefix_index])


def _contains_error_prefix(node: ast.AST) -> bool:
    static_string = _bounded_static_string(node)
    if static_string is not None:
        return static_string == "ERROR:"
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_contains_error_prefix(item) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _contains_error_prefix(node.left) or _contains_error_prefix(
            node.right
        )
    return False


def _bounded_static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _bounded_static_string(node.left)
        right = _bounded_static_string(node.right)
        if left is not None and right is not None and len(left) + len(right) <= 64:
            return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        pairs = ((node.left, node.right), (node.right, node.left))
        for text_node, count_node in pairs:
            text = _bounded_static_string(text_node)
            if (
                text is not None
                and isinstance(count_node, ast.Constant)
                and type(count_node.value) is int
                and 0 <= count_node.value <= 16
                and len(text) * count_node.value <= 64
            ):
                return text * count_node.value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.findings: list[Finding] = []

    def _scope_name(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _visit_scope(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    def visit_Return(self, node: ast.Return) -> None:
        if _literal_error_prefix(node.value):
            self.findings.append(Finding(
                self.relative_path,
                self._scope_name(),
                CATEGORY_RETURN,
                node.lineno,
                ast.unparse(node.value),
            ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _exact_error_startswith(node):
            self.findings.append(Finding(
                self.relative_path,
                self._scope_name(),
                CATEGORY_STARTSWITH,
                node.lineno,
                ast.unparse(node),
            ))
        self.generic_visit(node)


def production_files(root: Path) -> list[Path]:
    files = list(root.glob("*.py"))
    files.extend((root / "sonder_runtime").rglob("*.py"))
    files.extend((root / "scripts").rglob("*.py"))
    return sorted({
        path.resolve()
        for path in files
        if path.is_file() and path.resolve() != Path(__file__).resolve()
    })


def inventory(root: Path = REPO_ROOT) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    for path in production_files(root):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _Visitor(relative)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings


def baseline_from(findings: list[Finding]) -> dict:
    counts = Counter(finding.key for finding in findings)
    entries = [
        {
            "path": key[0], "scope": key[1], "category": key[2],
            "signal": key[3], "max_count": count,
        }
        for key, count in sorted(counts.items())
    ]
    return {"schema_version": 1, "entries": entries}


def load_baseline(path: Path = BASELINE_PATH) -> dict[tuple[str, str, str, str], int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
        raise ValueError("unsupported error-signal baseline schema")
    baseline = {}
    for entry in payload["entries"]:
        key = (entry["path"], entry["scope"], entry["category"], entry["signal"])
        count = entry["max_count"]
        if key in baseline or type(count) is not int or count < 1:
            raise ValueError("invalid or duplicate error-signal baseline entry")
        baseline[key] = count
    return baseline


def violations(
    findings: list[Finding],
    baseline: dict[tuple[str, str, str, str], int],
) -> list[str]:
    current = Counter(finding.key for finding in findings)
    details = {finding.key: finding for finding in findings}
    output = []
    for key, count in sorted(current.items()):
        allowed = baseline.get(key, 0)
        if count <= allowed:
            continue
        finding = details[key]
        output.append(
            "%s:%d: unexpected %s in %s (%d present, baseline allows %d): %s"
            % (
                finding.path, finding.line, finding.category, finding.scope,
                count, allowed, finding.expression,
            )
        )
    return output


def check(root: Path = REPO_ROOT, baseline_path: Path = BASELINE_PATH) -> list[str]:
    return violations(inventory(root), load_baseline(baseline_path))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--print-baseline"]:
        print(json.dumps(
            baseline_from(inventory()), sort_keys=True, separators=(",", ":"),
        ))
        return 0
    if argv:
        print("usage: check_error_signals.py [--print-baseline]", file=sys.stderr)
        return 2
    problems = check()
    if problems:
        print("legacy ERROR: signal ratchet failed; remove/migrate sites, do not add or swap them")
        print("\n".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
