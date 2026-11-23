"""Pure multi-language stacktrace and compiler-error parser & root-cause classifier.

Pure domain module: stdlib only, zero I/O. Parses Python tracebacks, MSVC
C####/LNK#### diagnostics, GCC/Clang errors, Rust error[E####], JS/TS stack
traces, and pytest failures into structured frames and actionable fix guidance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiagnosticFrame:
    file: str
    line: int
    column: int | None = None
    function: str = ""
    code: str = ""
    message: str = ""
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"file": self.file, "line": self.line}
        if self.column is not None:
            out["column"] = self.column
        if self.function:
            out["function"] = self.function
        if self.code:
            out["code"] = self.code
        if self.message:
            out["message"] = self.message
        if self.language:
            out["language"] = self.language
        return out


_PY_FRAME_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([^\r\n]+))?')
_PY_EXC_RE = re.compile(
    r"(?m)^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Exit|Interrupt))\s*:\s*(.+)$"
)
_MSVC_RE = re.compile(
    r"(?m)^([^\r\n(]+?)\((\d+)(?:,(\d+))?\)\s*:\s*(?:fatal\s+)?(error|warning)\s+(C\d{4}|LNK\d{4})\s*:\s*(.+)$"
)
_MSVC_LINK_ONLY_RE = re.compile(
    r"(?m)(?:fatal\s+)?error\s+(LNK\d{4})\s*:\s*(.+)$"
)
_GCC_CLANG_RE = re.compile(
    r"(?m)^([^\s:\r\n][^:\r\n]*\.(?:c|cc|cpp|cxx|h|hpp|hh|rs|go|java|kt|swift)):(\d+):(?:(\d+):)?\s*(?:fatal\s+)?error(?:\[(E\d{4})\])?:\s*(.+)$"
)
_RUST_LOC_RE = re.compile(r"(?m)^\s*-->\s*([^:\r\n]+):(\d+):(\d+)\s*$")
_RUST_ERR_RE = re.compile(r"(?m)^error(?:\[(E\d{4})\])?:\s*(.+)$")
_JS_FRAME_RE = re.compile(
    r"(?m)^\s*at\s+(?:([^\s(]+)\s+\()?([^():\r\n]+\.(?:[cm]?js|tsx?|jsx)):(\d+):(\d+)\)?\s*$"
)
_PYTEST_FAIL_RE = re.compile(r"(?m)^([^:\s\r\n]+\.py):(\d+):\s*(.+)$")


def _classify_root_cause(
    error_type: str,
    error_code: str,
    error_message: str,
    raw_text: str,
) -> tuple[str, list[str], list[str]]:
    """Return (category, probable_causes, suggested_steps)."""
    et = (error_type or "").lower()
    ec = (error_code or "").upper()
    em = f"{error_type} {error_code} {error_message} {raw_text[:800]}".lower()

    if ec.startswith("LNK") or "unresolved external symbol" in em:
        return (
            "linker_unresolved",
            [
                "A declared function/symbol has no compiled definition linked into the target.",
                "Missing source file in CMakeLists/build target, missing link library, or calling-convention/signature mismatch.",
            ],
            [
                "Verify the symbol's definition exists and its .cpp file is listed in the target's sources.",
                "Check header vs .cpp signature (const, namespaces, templates, extern \"C\") and target_link_libraries.",
            ],
        )
    if ec.startswith("C") or "syntax error" in em or et in ("syntaxerror", "indentationerror", "taberror"):
        return (
            "syntax_or_compile_error",
            [
                "Malformed syntax, missing delimiter/semicolon, or mismatched indentation.",
                "In C/C++, a missing header include or earlier macro/brace error often cascades into later lines.",
            ],
            [
                "Inspect the exact reported line AND the 2-3 lines directly above it (unclosed parens/brackets often report on the next line).",
                "Fix the FIRST compiler error first and rebuild; ignore cascading errors below it.",
            ],
        )
    if et in ("modulenotfounderror", "importerror") or "cannot find module" in em or "no module named" in em:
        return (
            "missing_import",
            [
                "The target module/package is not installed in the active interpreter/environment, or the import path is misspelled.",
                "Possible circular import or wrong working directory / PYTHONPATH.",
            ],
            [
                "Verify the exact package/module name with `symbol_verify` or `file_find`.",
                "Check which interpreter/virtualenv is running and whether the module exists in that environment.",
            ],
        )
    if et in ("attributeerror", "nameerror") or "has no attribute" in em or "is not defined" in em:
        return (
            "missing_symbol_or_attribute",
            [
                "Calling a method/attribute that does not exist on the actual runtime object type (or object is `None`).",
                "Typo in identifier, wrong import alias, or stale API signature.",
            ],
            [
                "Run `symbol_verify` on the module/class to confirm the real attribute names and signature.",
                "Inspect the failing frame to verify the variable's actual runtime type (check for unexpected `None` or tuple).",
            ],
        )
    if et in ("typeerror", "valueerror"):
        if "json serializable" in em:
            return (
                "serialization_error",
                [
                    "A non-JSON Python object (e.g. sentinel `object()`, Path, datetime, set, dataclass) reached `json.dumps()`.",
                ],
                [
                    "Normalize values before serialization (convert dataclasses/Paths/sets, strip sentinel objects, or pass a safe `default=` serializer).",
                ],
            )
        return (
            "type_or_argument_mismatch",
            [
                "Function received wrong argument count, unexpected keyword argument, or incompatible value type.",
                "Unpacking mismatch (e.g. expecting 2 values from a 3-tuple or indexing a non-subscriptable object).",
            ],
            [
                "Inspect the callee signature with `symbol_verify` or `file_read` on the definition.",
                "Check the exact types and shapes passed at the innermost repository frame.",
            ],
        )
    if et in ("keyerror", "indexerror") or "cannot read propert" in em or "undefined is not" in em:
        return (
            "missing_key_or_bounds",
            [
                "Dictionary key missing or list/array index out of range.",
                "Upstream data schema changed or an optional field was omitted.",
            ],
            [
                "Validate input shape with `schema_validate` or guard access with `.get(...)` / bounds checks.",
                "Check why the expected key/element was absent upstream rather than masking the bug.",
            ],
        )
    if et in ("filenotfounderror", "permissionerror", "oserror") or "enoent" in em or "access is denied" in em:
        return (
            "filesystem_or_permission",
            [
                "Referenced path does not exist, working directory differs from expectation, or file is locked/unpermitted.",
            ],
            [
                "Confirm the exact path via `file_find` or `directory_tree`.",
                "Verify relative paths are resolved against the expected root and parent directories exist.",
            ],
        )
    if et in ("assertionerror",) or "assert " in em or "failed" in em and "pytest" in em:
        return (
            "assertion_failure",
            [
                "Actual behavior diverged from the test/invariant expectation.",
            ],
            [
                "Compare actual vs expected values at the failing assertion line.",
                "Re-run only the single failing test case with verbose output to iterate quickly.",
            ],
        )
    if "timeout" in em or "connection refused" in em or "502" in em or "503" in em:
        return (
            "network_or_timeout",
            [
                "Target service is down, overloaded, listening on a different port, or timed out.",
            ],
            [
                "Check service health/status and verify host/port bindings before retrying.",
                "Use bounded retries or an offline fallback path via `plan_fallback`.",
            ],
        )
    return (
        "general_error",
        [
            "Runtime or build error in the reported innermost frame.",
        ],
        [
            "Inspect the innermost repository frame and surrounding lines.",
            "Run `code_audit` on the failing file and reproduce minimally with `test_run` or `run_code`.",
        ],
    )


def parse_error_report(error_text: str, *, max_frames: int = 12) -> dict[str, Any]:
    """Parse raw stacktrace / compiler diagnostics into structured frames and root cause."""
    text = str(error_text or "").strip()
    if not text:
        return {
            "ok": False,
            "error": "error_text is required",
            "frames": [],
        }

    frames: list[DiagnosticFrame] = []
    detected_lang = "unknown"
    error_type = ""
    error_code = ""
    error_message = ""

    # 1. Python tracebacks
    py_matches = list(_PY_FRAME_RE.finditer(text))
    if py_matches:
        detected_lang = "python"
        for m in py_matches:
            frames.append(DiagnosticFrame(
                file=m.group(1).strip(),
                line=int(m.group(2)),
                function=(m.group(3) or "").strip(),
                language="python",
            ))
    py_excs = list(_PY_EXC_RE.finditer(text))
    if py_excs:
        last_exc = py_excs[-1]
        error_type = last_exc.group(1).strip()
        error_message = last_exc.group(2).strip()
        if detected_lang == "unknown":
            detected_lang = "python"

    # 2. MSVC compiler/linker errors
    msvc_matches = list(_MSVC_RE.finditer(text))
    if msvc_matches:
        detected_lang = "cpp_msvc"
        for m in msvc_matches:
            if m.group(4).lower() != "error":
                continue
            code = m.group(5).upper()
            msg = m.group(6).strip()
            frames.append(DiagnosticFrame(
                file=m.group(1).strip(),
                line=int(m.group(2)),
                column=int(m.group(3)) if m.group(3) else None,
                code=code,
                message=msg,
                language="cpp",
            ))
            if not error_code:
                error_code = code
                error_type = f"MSVC {code}"
                error_message = msg
    elif not error_code:
        lnk = _MSVC_LINK_ONLY_RE.search(text)
        if lnk:
            detected_lang = "cpp_msvc"
            error_code = lnk.group(1).upper()
            error_type = f"MSVC {error_code}"
            error_message = lnk.group(2).strip()

    # 3. GCC/Clang/Go/Rust single-line errors
    if not frames:
        gcc_matches = list(_GCC_CLANG_RE.finditer(text))
        if gcc_matches:
            detected_lang = "compiler"
            for m in gcc_matches:
                code = (m.group(4) or "").upper()
                msg = m.group(5).strip()
                frames.append(DiagnosticFrame(
                    file=m.group(1).strip(),
                    line=int(m.group(2)),
                    column=int(m.group(3)) if m.group(3) else None,
                    code=code,
                    message=msg,
                    language="compiler",
                ))
                if not error_message:
                    error_code = code
                    error_type = f"CompilerError {code}".strip()
                    error_message = msg

    # 4. Rust --> file:line:col blocks
    rust_locs = list(_RUST_LOC_RE.finditer(text))
    if rust_locs and not frames:
        detected_lang = "rust"
        rerr = _RUST_ERR_RE.search(text)
        if rerr:
            error_code = (rerr.group(1) or "").upper()
            error_type = f"Rust {error_code}".strip()
            error_message = rerr.group(2).strip()
        for m in rust_locs:
            frames.append(DiagnosticFrame(
                file=m.group(1).strip(),
                line=int(m.group(2)),
                column=int(m.group(3)),
                code=error_code,
                message=error_message,
                language="rust",
            ))

    # 5. JavaScript / TypeScript / Node stack frames
    if not frames:
        js_matches = list(_JS_FRAME_RE.finditer(text))
        if js_matches:
            detected_lang = "javascript"
            for m in js_matches:
                frames.append(DiagnosticFrame(
                    file=m.group(2).strip(),
                    line=int(m.group(3)),
                    column=int(m.group(4)),
                    function=(m.group(1) or "<anonymous>").strip(),
                    language="javascript",
                ))

    # 6. Pytest short summary lines (file.py:123: AssertionError)
    if not frames:
        pt_matches = list(_PYTEST_FAIL_RE.finditer(text))
        if pt_matches:
            detected_lang = "python"
            for m in pt_matches:
                frames.append(DiagnosticFrame(
                    file=m.group(1).strip(),
                    line=int(m.group(2)),
                    message=m.group(3).strip(),
                    language="python",
                ))
                if not error_message:
                    error_message = m.group(3).strip()

    if not error_message:
        non_empty = [ln.strip() for ln in text.splitlines() if ln.strip()]
        error_message = non_empty[-1][:240] if non_empty else ""

    category, causes, steps = _classify_root_cause(error_type, error_code, error_message, text)

    # Deduplicate frames while preserving order
    deduped: list[DiagnosticFrame] = []
    seen: set[tuple[str, int, str]] = set()
    for fr in frames:
        key = (fr.file, fr.line, fr.function)
        if key not in seen:
            seen.add(key)
            deduped.append(fr)

    # For Python/JS, the innermost frame is last; for compilers, first error is primary.
    primary_frame = None
    if deduped:
        primary_frame = deduped[-1] if detected_lang in ("python", "javascript") else deduped[0]

    return {
        "ok": True,
        "language": detected_lang,
        "category": category,
        "error_type": error_type or category,
        "error_code": error_code,
        "error_message": error_message,
        "frame_count": len(deduped),
        "frames": [f.to_dict() for f in deduped[:max_frames]],
        "primary_frame": primary_frame.to_dict() if primary_frame else None,
        "probable_causes": causes,
        "suggested_steps": steps,
    }
