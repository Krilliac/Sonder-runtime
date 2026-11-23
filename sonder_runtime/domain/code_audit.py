"""Zero-dependency static security & bug-footgun auditor (Python AST + generic patterns).

Pure domain module: stdlib only, zero I/O. Designed to catch real security
vulnerabilities and classic correctness traps before code is shipped.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class AuditFinding:
    rule_id: str
    severity: str
    category: str
    cwe: str
    line: int
    message: str
    remediation: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "cwe": self.cwe,
            "line": self.line,
            "message": self.message,
            "remediation": self.remediation,
            "snippet": self.snippet,
        }


def _line_snippet(lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:140]
    return ""


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_constant_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_constant_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _kw_value(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_dynamic_sql_arg(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return True
    return False


def _looks_like_sql(node: ast.AST) -> bool:
    sql_rx = re.compile(r"(?i)\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b")
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sql_rx.search(sub.value):
            return True
    return False


def _audit_python_ast(code: str, lines: list[str]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        lineno = int(exc.lineno or 1)
        findings.append(AuditFinding(
            rule_id="PY000",
            severity="critical",
            category="syntax",
            cwe="",
            line=lineno,
            message=f"Python syntax error: {exc.msg}",
            remediation="Fix the syntax error before executing or committing this file.",
            snippet=_line_snippet(lines, lineno),
        ))
        return findings

    def add(rule_id: str, severity: str, category: str, cwe: str, lineno: int, msg: str, fix: str) -> None:
        findings.append(AuditFinding(
            rule_id=rule_id,
            severity=severity,
            category=category,
            cwe=cwe,
            line=lineno,
            message=msg,
            remediation=fix,
            snippet=_line_snippet(lines, lineno),
        ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            lineno = getattr(node, "lineno", 1)

            if name in ("eval", "exec"):
                add(
                    "SEC101", "critical", "code_injection", "CWE-95", lineno,
                    f"Use of dynamic `{name}()` executes arbitrary code.",
                    "Replace with ast.literal_eval() for data parsing or an explicit dispatch table.",
                )
            elif name in ("pickle.loads", "pickle.load", "cPickle.loads", "marshal.loads", "shelve.open"):
                add(
                    "SEC102", "high", "unsafe_deserialization", "CWE-502", lineno,
                    f"`{name}()` can execute arbitrary code on untrusted input.",
                    "Use JSON or a schema-validated format for untrusted or persisted data.",
                )
            elif name == "yaml.load":
                loader = _kw_value(node, "Loader")
                loader_name = _dotted_name(loader) if loader is not None else ""
                if "SafeLoader" not in loader_name and "CSafeLoader" not in loader_name:
                    add(
                        "SEC103", "high", "unsafe_deserialization", "CWE-502", lineno,
                        "`yaml.load()` without SafeLoader allows arbitrary Python object instantiation.",
                        "Use `yaml.safe_load()` or pass `Loader=yaml.SafeLoader`.",
                    )
            elif name in ("os.system", "os.popen"):
                add(
                    "SEC104", "high", "command_injection", "CWE-78", lineno,
                    f"`{name}()` invokes a system shell and is vulnerable to shell injection.",
                    "Use `subprocess.run([...], shell=False)` with an explicit argument list.",
                )
            elif name in (
                "subprocess.run", "subprocess.Popen", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output",
            ):
                if _is_constant_true(_kw_value(node, "shell")):
                    add(
                        "SEC105", "high", "command_injection", "CWE-78", lineno,
                        f"`{name}(..., shell=True)` invokes a shell and risks command injection.",
                        "Pass a list of command arguments with `shell=False`.",
                    )
            elif name.endswith(".execute") or name.endswith(".executemany"):
                if node.args and _is_dynamic_sql_arg(node.args[0]) and _looks_like_sql(node.args[0]):
                    add(
                        "SEC106", "critical", "sql_injection", "CWE-89", lineno,
                        "Dynamic string construction inside SQL `.execute(...)` risks SQL injection.",
                        "Use parameterized queries (e.g. `cursor.execute('SELECT ... WHERE id = ?', (val,))`).",
                    )
            elif name in ("hashlib.md5", "hashlib.sha1"):
                usedforsec = _kw_value(node, "usedforsecurity")
                if not _is_constant_false(usedforsec):
                    add(
                        "SEC107", "medium", "weak_crypto", "CWE-328", lineno,
                        f"`{name}()` is cryptographically broken if used for security.",
                        "Use `hashlib.sha256()` (or pass `usedforsecurity=False` for non-security checksums).",
                    )
            elif name in ("tempfile.mktemp",):
                add(
                    "SEC108", "high", "insecure_temp", "CWE-377", lineno,
                    "`tempfile.mktemp()` has a race condition (TOCTOU).",
                    "Use `tempfile.NamedTemporaryFile()` or `tempfile.mkstemp()`.",
                )
            elif _is_constant_false(_kw_value(node, "verify")) and (
                name.startswith("requests.") or name.startswith("httpx.") or name.endswith(".get") or name.endswith(".post")
            ):
                add(
                    "SEC109", "high", "tls_bypass", "CWE-295", lineno,
                    "TLS certificate verification disabled (`verify=False`).",
                    "Remove `verify=False` or pass a verified CA bundle path.",
                )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            for default in defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    add(
                        "BUG201", "high", "mutable_default", "", getattr(default, "lineno", node.lineno),
                        f"Function `{node.name}` uses a mutable default argument (`{type(default).__name__.lower()}`).",
                        "Default to `None` and initialize a fresh container inside the function body.",
                    )
            # Check for unreachable statements in any statement block inside the function
            for sub in ast.walk(node):
                for attr in ("body", "orelse", "finalbody"):
                    stmts = getattr(sub, attr, None)
                    if isinstance(stmts, list) and len(stmts) >= 2:
                        for idx, stmt in enumerate(stmts[:-1]):
                            if isinstance(stmt, (ast.Return, ast.Raise)):
                                nxt = stmts[idx + 1]
                                add(
                                    "BUG205", "medium", "unreachable_code", "CWE-561", getattr(nxt, "lineno", stmt.lineno),
                                    f"Unreachable code in `{node.name}` after `{type(stmt).__name__.lower()}`.",
                                    "Remove the dead statement or fix the control-flow condition.",
                                )
                                break

        elif isinstance(node, ast.ExceptHandler):
            lineno = getattr(node, "lineno", 1)
            is_pass_only = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if node.type is None:
                add(
                    "BUG202", "high" if is_pass_only else "medium", "bare_except", "CWE-391", lineno,
                    "Bare `except:` catches SystemExit and KeyboardInterrupt as well as bugs.",
                    "Catch a specific exception class (or `Exception`) and log/handle it explicitly.",
                )
            elif is_pass_only and isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
                add(
                    "BUG203", "medium", "swallowed_exception", "CWE-391", lineno,
                    f"`except {node.type.id}: pass` silently swallows failures.",
                    "Narrow the exception type or record/report the failure.",
                )

        elif isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Tuple) and len(node.test.elts) > 0:
                add(
                    "BUG204", "high", "always_true_assert", "", getattr(node, "lineno", 1),
                    "`assert (cond, msg)` asserts on a non-empty tuple, which is ALWAYS truthy!",
                    "Remove the outer parentheses: `assert cond, msg`.",
                )

        elif isinstance(node, ast.Compare):
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comp, ast.Constant):
                    if comp.value not in (None, True, False):
                        add(
                            "BUG206", "medium", "identity_literal_compare", "", getattr(node, "lineno", 1),
                            f"Using `is`/`is not` to compare against literal `{comp.value!r}`.",
                            "Use `==` or `!=` for value comparison (reserve `is` for `None`/`True`/`False`).",
                        )

        elif isinstance(node, ast.Dict):
            seen_keys: set[Any] = set()
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, (str, int, float, bool)):
                    tagged = (type(k.value), k.value)
                    if tagged in seen_keys:
                        add(
                            "BUG207", "medium", "duplicate_dict_key", "", getattr(k, "lineno", getattr(node, "lineno", 1)),
                            f"Duplicate dictionary key `{k.value!r}` silently overwrites earlier entry.",
                            "Remove or rename the duplicate key.",
                        )
                    seen_keys.add(tagged)

    return findings


_GENERIC_RULES: tuple[tuple[str, str, str, str, re.Pattern[str], str, str], ...] = (
    (
        "GEN301", "critical", "hardcoded_secret", "CWE-798",
        re.compile(r"""(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"""),
        "Possible hardcoded secret or private key found in source.",
        "Move credentials to environment variables or a secret store and rotate if committed.",
    ),
    (
        "GEN302", "high", "js_xss_or_eval", "CWE-79",
        re.compile(r"""\b(?:dangerouslySetInnerHTML|innerHTML\s*=|document\.write\s*\()"""),
        "Direct HTML injection sink detected.",
        "Use safe text rendering (`textContent`) or sanitize untrusted HTML before insertion.",
    ),
    (
        "GEN303", "high", "tls_bypass", "CWE-295",
        re.compile(r"""(?:rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['"]?0)"""),
        "TLS certificate verification is disabled.",
        "Enable TLS certificate verification and configure trusted CA roots instead.",
    ),
    (
        "GEN304", "high", "unsafe_shell", "CWE-78",
        re.compile(r"""(?i)-ExecutionPolicy\s+Bypass\b"""),
        "PowerShell `-ExecutionPolicy Bypass` weakens host execution policy.",
        "Run scripts with `-NoProfile -File` without bypassing execution policy.",
    ),
)


def audit_source(
    code: str,
    *,
    filename: str = "",
    language: str = "",
    min_severity: str = "low",
    max_findings: int = 80,
) -> dict[str, Any]:
    """Audit inline or file source code for security and correctness issues."""
    src = str(code or "")
    lines = src.splitlines()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    lang = (language or "").strip().lower()
    if not lang:
        if ext in ("py", "pyi", "pyw"):
            lang = "python"
        elif ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
            lang = "javascript"
        elif ext in ("c", "cc", "cpp", "cxx", "h", "hpp"):
            lang = "cpp"
        elif ext in ("ps1", "psm1"):
            lang = "powershell"
        elif ext in ("rs",):
            lang = "rust"
        elif ext in ("go",):
            lang = "go"
        else:
            # Heuristic: try Python if it looks like Python
            if re.search(r"(?m)^\s*(?:def |class |import |from \S+ import )", src):
                lang = "python"
            else:
                lang = "generic"

    findings: list[AuditFinding] = []
    if lang == "python":
        findings.extend(_audit_python_ast(src, lines))

    for rule_id, severity, category, cwe, pattern, msg, fix in _GENERIC_RULES:
        for match in pattern.finditer(src):
            lineno = src.count("\n", 0, match.start()) + 1
            findings.append(AuditFinding(
                rule_id=rule_id,
                severity=severity,
                category=category,
                cwe=cwe,
                line=lineno,
                message=msg,
                remediation=fix,
                snippet=_line_snippet(lines, lineno),
            ))

    threshold = _SEVERITY_ORDER.get((min_severity or "low").lower(), 1)
    filtered = [f for f in findings if _SEVERITY_ORDER.get(f.severity, 0) >= threshold]
    filtered.sort(key=lambda f: (-_SEVERITY_ORDER.get(f.severity, 0), f.line, f.rule_id))
    filtered = filtered[:max_findings]

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in filtered:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    return {
        "ok": True,
        "language": lang,
        "filename": filename,
        "clean": len(filtered) == 0,
        "finding_count": len(filtered),
        "severity_counts": counts,
        "findings": [f.to_dict() for f in filtered],
    }


def format_proactive_audit_notice(code: str, *, filename: str = "") -> str:
    """Return a concise notice for ChatToolTurn when written Python has high/critical issues."""
    if not code or (filename and not filename.lower().endswith((".py", ".pyi", ".pyw"))):
        return ""
    report = audit_source(code, filename=filename or "snippet.py", language="python", min_severity="high", max_findings=5)
    if report["clean"]:
        return ""
    lines = [
        f"\n[PROACTIVE CODE AUDIT: {report['finding_count']} high/critical issue(s) detected in {filename or 'written code'}]"
    ]
    for f in report["findings"]:
        lines.append(f"  - [{f['severity'].upper()} {f['rule_id']}] line {f['line']}: {f['message']} Fix: {f['remediation']}")
    return "\n".join(lines)
