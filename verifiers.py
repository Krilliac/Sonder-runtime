"""verifiers — a pluggable grounding registry so the generate->verify->repair
principle applies BEYOND Python games.

The insight from the game gauntlet: solver.solve(), run_ladder_repair(), and
reward.record_outcome() are all *verifier-agnostic* — they take the pass/fail
oracle as an injected dependency. So "does grounding apply everywhere?" reduces
to "how many verifier backends have we registered?". Each backend maps a produced
artifact + a task spec to a Verdict; wiring a new domain = adding one function
here, not touching the loops. solver.solve_verified() is the single seam that
drives self-repair off any registered verifier.

A verifier: fn(artifact: str, spec: dict) -> Verdict(passed, reason, detail)
  artifact — the model's output (code, a program, a patch)
  spec     — task context, verifier-specific (documented per backend below)
  detail   — the FULL diagnostic (traceback/compiler output) for the repair loop;
             `reason` is the one-line summary for logging.

Raises VerifierUnavailable when a backend's external tool (compiler, mypy) is
absent — that is "could not judge", distinct from a Verdict(False) "artifact failed".
"""
import collections
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

import grounding
import isolated_runner
import sonder_logging
from sonder_runtime.adapters.execution_tools import code_runner

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])

# Genuinely future backends (documented surface, not yet implemented).
PLANNED = {
    "fuzz": "run a fuzzer against the artifact; passed iff no crash within budget",
    "benchmark_perf": "run + time the artifact; passed iff within a perf threshold",
}

# cpp_compile interpolates these into an executed .bat, so they are validated:
_ALLOWED_CPP_STD = {"c++11", "c++14", "c++17", "c++20", "c++23", "c++latest"}
_BAT_META = set('&|<>^"%\r\n')
_MAX_LEAN_SOURCE_BYTES = 256_000
_MAX_LEAN_DETAIL_CHARS = 8_000
_MAX_LEAN_EXPECTED_TYPE_BYTES = 8_192
_MAX_LEAN_TRUSTED_PRELUDE_BYTES = 32_768
_LEAN_SANDBOX_IMAGE_ENV = "SONDER_LEAN_SANDBOX_IMAGE"
_LEAN_SANDBOX_ROOT_ENV = "SONDER_LEAN_SANDBOX_ROOT"
_LEAN_SANDBOX_EXECUTABLE_ENV = "SONDER_LEAN_SANDBOX_EXECUTABLE"
_LEAN_SANDBOX_EXECUTABLE_RE = re.compile(r"(?:/[A-Za-z0-9._+-]+)+|[A-Za-z0-9._+-]+\Z")
_LEAN_TRUST_GAP_RE = re.compile(r"\b(sorryAx|sorry|admit|axiom|constant)\b")
_LEAN_DECLARATION_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z"
)
_LEAN_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(?:(all)[ \t]+)?"
    r"([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)[ \t]*$",
    re.MULTILINE,
)
_LEAN_VERSION_RE = re.compile(
    r"\blean\b.*?\bversion\s+([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)",
    re.IGNORECASE,
)
_LEAN_PIN_VERSION_RE = re.compile(
    r"(?:^|:)v?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\Z"
)
_LEAN_AXIOM_AUDIT_REJECTION = "Sonder rejected unproved axiom dependencies:"
_LEAN_CONTRACT_AUDIT_REJECTION = "Sonder rejected theorem contract type mismatch:"
_LEAN_AXIOM_AUDITOR_SOURCE = r'''import Lean

open Lean

def audit (moduleName declaration expectedModule expectedDeclaration : Name) : IO UInt32 := do
  let env <- importModules #[{ module := expectedModule }, { module := moduleName }] {}
  let some actualInfo := env.find? declaration
    | IO.eprintln s!"Sonder rejected theorem contract type mismatch: missing declaration {declaration}"; return 1
  let some expectedInfo := env.find? expectedDeclaration
    | IO.eprintln s!"Sonder contract audit could not locate trusted declaration {expectedDeclaration}"; return 2
  let typesMatch <- Lean.Core.CoreM.toIO'
    ((Lean.Meta.isDefEq actualInfo.type expectedInfo.type).run')
    { fileName := "<sonder-contract-audit>", fileMap := default }
    { env := env }
  if !typesMatch then
    IO.eprintln s!"Sonder rejected theorem contract type mismatch: {declaration} has type {actualInfo.type}, expected {expectedInfo.type}"
    return 1
  let axioms <- Lean.Core.CoreM.toIO' (collectAxioms declaration)
    { fileName := "<sonder-axiom-audit>", fileMap := default }
    { env := env }
  let some moduleIdx := env.getModuleIdx? moduleName
    | IO.eprintln s!"Sonder axiom audit could not locate module {moduleName}"; return 2
  let untrusted := axioms.filter fun name =>
    name == ``sorryAx || name == expectedDeclaration ||
      env.getModuleIdxFor? name == some moduleIdx
  if untrusted.isEmpty then
    return 0
  IO.eprintln s!"Sonder rejected unproved axiom dependencies: {untrusted.toList}"
  return 1

def main (args : List String) : IO UInt32 :=
  match args with
  | [moduleName, declaration, expectedModule, expectedDeclaration] =>
      audit moduleName.toName declaration.toName expectedModule.toName expectedDeclaration.toName
  | _ => do
    IO.eprintln "Sonder contract audit requires artifact and expected declarations"
    return 2
'''


class VerifierUnavailable(RuntimeError):
    """The verifier's external tool isn't present — 'could not judge', not 'failed'."""


# What the OS/shell prints when it cannot find the executable at all, as opposed
# to the executable running and rejecting the artifact. Same list node_verifier
# uses (node_verifier.py:27) — the two backends face the same distinction.
_TOOL_MISSING_MARKERS = (
    "is not recognized as an internal or external command",  # Windows cmd
    "No such file or directory",  # POSIX
    "command not found",
)


def _last_line(text):
    lines = [row for row in (text or "").strip().splitlines() if row.strip()]
    return lines[-1] if lines else ""


def _run(cmd, cwd=None, timeout=180, shell=False, env_overrides=None):
    environment = sonder_logging.child_environment()
    if env_overrides:
        environment.update(env_overrides)
    p = subprocess.run(
        cmd, cwd=cwd, capture_output=True, timeout=timeout, shell=shell,
        env=environment,
    )
    out = ((p.stdout or b"").decode("utf-8", "replace")
           + (p.stderr or b"").decode("utf-8", "replace"))
    return p.returncode, out


# --- python: execute code + an assert-check --------------------------------
def python_exec(artifact, spec=None):
    """spec={'check': <assert lines>}. Runs code+check in a subprocess."""
    check = (spec or {}).get("check", "")
    ok, out = grounding.run_code(artifact, check)
    return Verdict(ok, "passed" if ok else (_last_line(out) or "failed"), out)


# --- program: run a whole program headless, fail on crash ------------------
def program_run(artifact, spec=None):
    """spec={'kind': 'console'|'pygame'}. Runs the program; fails on real crash."""
    import game_ladder  # local import avoids an import-time cycle
    kind = (spec or {}).get("kind", "console")
    passed, reason, full = game_ladder._ground_capture(artifact, kind)
    return Verdict(passed, reason, full)


# --- pytest: run a repo's tests --------------------------------------------
def pytest_run(artifact, spec=None):
    """spec={'cwd': dir, 'select': nodeid?, 'write_to': path?, 'python': exe?}.
    If write_to is given, the artifact is written there first (module under test)."""
    spec = spec or {}
    cwd = spec.get("cwd") or "."
    write_to = spec.get("write_to")
    if write_to and artifact:
        # confine the write under cwd — reject traversal / absolute-path escapes
        base = os.path.abspath(cwd)
        dest = os.path.abspath(os.path.join(base, write_to))
        try:
            inside = os.path.commonpath([base, dest]) == base
        except ValueError:  # different drive on Windows
            inside = False
        if not inside:
            raise ValueError("write_to escapes cwd: %r" % (write_to,))
        with open(dest, "w", encoding="utf-8") as f:
            f.write(artifact)
    interp = spec.get("python", sys.executable)
    args = [interp, "-m", "pytest", "-q"]
    select = spec.get("select")
    if select:
        if str(select).startswith("-"):
            raise ValueError("select must be a test path/nodeid, not an option: %r" % (select,))
        args.append(str(select))
    rc, out = _run(args, cwd=cwd, timeout=spec.get("timeout", 300))
    return Verdict(rc == 0, "passed" if rc == 0 else (_last_line(out) or "pytest failed"),
                   out[-4000:])


# --- typecheck: mypy as a cheap partial oracle -----------------------------
def typecheck(artifact, spec=None):
    """spec={'python': exe?}. Runs mypy on the artifact; VerifierUnavailable if mypy absent."""
    interp = (spec or {}).get("python", sys.executable)
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(artifact)
        rc, out = _run([interp, "-m", "mypy", "--no-error-summary", "--no-color-output", path],
                       timeout=120)
        if "No module named mypy" in out or "No module named 'mypy'" in out:
            raise VerifierUnavailable("mypy not installed")
        return Verdict(rc == 0, "passed" if rc == 0 else (_last_line(out) or "type errors"),
                       out[-4000:])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- cpp: compile a single translation unit via MSVC/vcvars ----------------
def cpp_compile(artifact, spec=None):
    """spec={'vcvars': path?, 'std': 'c++17'?}. Compile-only (/c) via vcvars;
    VerifierUnavailable if vcvars64.bat is missing."""
    spec = spec or {}
    vcvars = spec.get("vcvars") or code_runner._find_visual_studio_vcvars()
    if not vcvars:
        raise VerifierUnavailable("vcvars64.bat was not discovered")
    vcvars = os.fspath(vcvars)
    # vcvars is interpolated into a batch `call`; require a real file with no shell
    # metacharacters to block command injection via a crafted spec['vcvars'].
    if not os.path.isfile(vcvars) or (_BAT_META & set(vcvars)):
        raise VerifierUnavailable("vcvars64.bat not found or unsafe path: %r" % (vcvars,))
    std = spec.get("std", "c++17")
    # std is also interpolated into the batch line — allowlist it (no injection).
    if std not in _ALLOWED_CPP_STD:
        raise ValueError("unsupported /std %r (allowed: %s)" % (std, sorted(_ALLOWED_CPP_STD)))
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "tu.cpp")  # our own mkdtemp path — not caller-controlled
        with open(src, "w", encoding="utf-8") as f:
            f.write(artifact)
        # Run through a .bat: `cmd /c "call \"path with spaces\" && cl ..."` gets its
        # outer quotes stripped by cmd and mangles the vcvars path — a wrapper file dodges it.
        bat = os.path.join(d, "build.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write('@echo off\r\ncall "%s" >nul\r\ncl /nologo /EHsc /std:%s /c "%s"\r\n'
                    % (vcvars, std, src))
        rc, out = _run(["cmd", "/c", bat], cwd=d, timeout=spec.get("timeout", 180))
        if rc == 0:
            reason = "compiled"
        else:
            # prefer the real MSVC diagnostic over trailing vcvars noise (vswhere, etc.)
            errs = [row.strip() for row in out.splitlines()
                    if "): error" in row or "error C" in row]
            if not errs and any(m in out for m in _TOOL_MISSING_MARKERS):
                # vcvars64.bat exists but there is no x64 toolset behind it (the
                # VC.CoreBuildTools case), or vcvars aborted and its diagnostic
                # went to the `>nul` above: cmd exits 9009 having printed only
                # its own not-found message. With no MSVC diagnostic to quote,
                # this returned Verdict(False, "operable program or batch file.")
                # — "could not judge" reported as "the artifact FAILED", so every
                # C++ artifact including correct ones was judged failed and fed
                # to solver's repair loop, burning the repair budget and writing
                # false-negative reward rows. The isfile() guard above only
                # covers the "no Visual Studio at all" case.
                raise VerifierUnavailable(
                    "MSVC cl.exe not usable via %r: %s" % (vcvars, _last_line(out)))
            reason = errs[0] if errs else (_last_line(out) or "compile error")
        return Verdict(rc == 0, reason, out[-4000:])
    finally:
        # Nothing removed this directory on ANY path — success, compile failure,
        # or the TimeoutExpired _run propagates — and it holds tu.cpp, build.bat
        # and any .obj. The test suite alone had left 192 of them in %TEMP%.
        shutil.rmtree(d, ignore_errors=True)


# --- Lean 4: kernel-check one self-contained formal proof -----------------
def _lean_code_only(source):
    """Blank comments and strings while preserving token boundaries/newlines.

    Lean block comments nest. A regular expression either misses nested trust
    gaps or flags harmless words in prose, so this deliberately tiny lexer
    handles only the syntax needed for a conservative placeholder scan.
    """
    output = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        pair = source[index:index + 2]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                output.extend("  ")
                index += 2
                continue
            if pair == "-/":
                block_depth -= 1
                output.extend("  ")
                index += 2
                continue
            output.append("\n" if char == "\n" else " ")
            index += 1
            continue
        if in_string:
            output.append("\n" if char == "\n" else " ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if pair == "--":
            output.extend("  ")
            index += 2
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if pair == "/-":
            block_depth = 1
            output.extend("  ")
            index += 2
            continue
        if char == '"':
            in_string = True
            output.append(" ")
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _configured_executable(configured, environment_name, default, label):
    value = str(
        configured
        or os.environ.get(environment_name, "").strip()
        or default
    ).strip()
    if not value or "\x00" in value:
        raise ValueError("%s executable must be a non-empty program name" % label)
    executable = shutil.which(value)
    if not executable:
        raise VerifierUnavailable("%s executable was not discovered: %r" % (label, value))
    return executable


def _lean_executable(configured):
    return _configured_executable(
        configured, "SONDER_LEAN_EXE", "lean", "Lean 4",
    )


def _lake_executable(configured):
    return _configured_executable(
        configured, "SONDER_LAKE_EXE", "lake", "Lake",
    )


def _lean_project(configured):
    value = configured
    if value is None:
        value = os.environ.get("SONDER_LEAN_PROJECT", "").strip()
    if value in (None, ""):
        return None
    try:
        root = os.path.abspath(os.path.expanduser(os.fspath(value)))
    except TypeError as exc:
        raise ValueError("Lean project must be a filesystem path") from exc
    if "\x00" in root:
        raise ValueError("Lean project path contains a null byte")
    if not os.path.isdir(root):
        raise VerifierUnavailable("configured Lean project is not a directory: %r" % root)
    if not any(
        os.path.isfile(os.path.join(root, name))
        for name in ("lakefile.toml", "lakefile.lean")
    ):
        raise VerifierUnavailable(
            "configured Lean project has no lakefile.toml or lakefile.lean: %r" % root
        )
    return root


def lean_contract(spec, required=False):
    """Validate and normalize an optional task-level Lean theorem contract."""
    has_declaration = "expected_declaration" in spec
    has_type = "expected_type" in spec
    if has_declaration != has_type:
        raise ValueError(
            "expected_declaration and expected_type must be supplied together"
        )
    if not has_declaration:
        if required:
            raise ValueError(
                "expected_declaration and expected_type are required"
            )
        return None

    declaration = spec["expected_declaration"]
    expected_type = spec["expected_type"]
    if not isinstance(declaration, str) or not _LEAN_DECLARATION_RE.fullmatch(
        declaration.strip()
    ):
        raise ValueError(
            "expected_declaration must be a qualified Lean identifier"
        )
    if not isinstance(expected_type, str) or not expected_type.strip():
        raise ValueError("expected_type must be a non-empty Lean type expression")
    expected_type = expected_type.strip()
    if "\x00" in expected_type:
        raise ValueError("expected_type contains a null byte")
    if len(expected_type.encode("utf-8")) > _MAX_LEAN_EXPECTED_TYPE_BYTES:
        raise ValueError("expected_type exceeds the 8192-byte verifier ceiling")
    return declaration.strip(), expected_type


def lean_trusted_prelude(spec):
    """Validate caller-owned Lean definitions shared with a proof contract.

    This source belongs to the task author, never to the generated artifact. It
    is compiled into its own module so contract-local predicates and structures
    can be named without exposing the trusted contract axiom to the submission.
    """
    if "trusted_prelude" not in spec:
        return None
    source = spec["trusted_prelude"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("trusted_prelude must be non-empty Lean source")
    if "\x00" in source:
        raise ValueError("trusted_prelude contains a null byte")
    if len(source.encode("utf-8")) > _MAX_LEAN_TRUSTED_PRELUDE_BYTES:
        raise ValueError("trusted_prelude exceeds the 32768-byte verifier ceiling")
    return source if source.endswith("\n") else source + "\n"


def _repository_lean_toolchain():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lean-toolchain")
    try:
        with open(path, encoding="utf-8") as handle:
            pin = handle.read().strip()
    except OSError as exc:
        raise VerifierUnavailable("repository lean-toolchain pin is unavailable") from exc
    match = _LEAN_PIN_VERSION_RE.search(pin)
    if not pin or "\n" in pin or not match:
        raise VerifierUnavailable("repository lean-toolchain pin is invalid")
    return pin, match.group(1)


def _lean_import_lines(artifact):
    """Return deduplicated, code-only import declarations from an artifact."""
    imports = []
    seen = set()
    for match in _LEAN_IMPORT_RE.finditer(_lean_code_only(artifact)):
        line = "import %s%s" % (
            "all " if match.group(1) else "",
            match.group(2),
        )
        if line not in seen:
            imports.append(line)
            seen.add(line)
    return imports


def _lean_trusted_prelude_source(artifact, trusted_prelude):
    """Build the caller-owned prelude with the artifact's safe imports."""
    prefix = "\n".join(_lean_import_lines(artifact))
    if prefix:
        prefix += "\n\n"
    return prefix + trusted_prelude


def _lean_expected_contract_source(
    artifact, expected_type, module_name, prelude_module=None,
):
    """Build a trusted type declaration outside the submitted module."""
    if prelude_module:
        prefix = "import %s\n\n" % prelude_module
    else:
        prefix = "\n".join(_lean_import_lines(artifact))
        if prefix:
            prefix += "\n\n"
    return (
        prefix
        + "-- Generated outside the submitted module; its macros cannot rewrite this contract.\n"
        + "namespace %s\n" % module_name
        + "axiom contract : (%s)\n" % expected_type
        + "end %s\n" % module_name
    )


def _lean_sandbox_image():
    """Return the operator-configured, locally inspected Lean image reference."""
    image = os.environ.get(_LEAN_SANDBOX_IMAGE_ENV, "").strip()
    if not image:
        raise VerifierUnavailable(
            "Lean verification requires a configured sandbox image (%s)"
            % _LEAN_SANDBOX_IMAGE_ENV
        )
    return image


def _lean_sandbox_executable():
    executable = os.environ.get(_LEAN_SANDBOX_EXECUTABLE_ENV, "lean").strip()
    if not _LEAN_SANDBOX_EXECUTABLE_RE.fullmatch(executable):
        raise VerifierUnavailable("configured Lean sandbox executable is invalid")
    return executable


def _lean_sandbox_root():
    root = os.environ.get(_LEAN_SANDBOX_ROOT_ENV, "").strip()
    if not root:
        raise VerifierUnavailable(
            "Lean verification requires a configured sandbox root (%s)"
            % _LEAN_SANDBOX_ROOT_ENV
        )
    try:
        return isolated_runner.resolve_project(root)
    except ValueError as exc:
        raise VerifierUnavailable(
            "configured Lean sandbox root is unavailable: %s" % exc
        ) from exc


def _sandbox_lean_argument(value, directory):
    """Map only verifier-owned paths into the container workspace."""
    raw = str(value)
    try:
        relative = os.path.relpath(raw, directory)
    except ValueError:
        return raw
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return raw
    return "/workspace" if relative == "." else "/workspace/" + relative.replace("\\", "/")


def _run_lean_in_sandbox(command, *, cwd, timeout, env_overrides=None):
    """Run a verifier-owned Lean argv through the guarded OCI executor.

    The submitted source is never compiled by a host process.  The only host
    mount is an empty verifier-owned temporary directory; the executor applies
    no network, a read-only container root, non-root UID, dropped capabilities,
    bounded resources, and immutable image inspection.  ``env_overrides`` is
    deliberately ignored because a sandboxed process receives a fixed minimal
    environment rather than the runtime's environment.
    """
    del env_overrides
    image = _lean_sandbox_image()
    root = _lean_sandbox_root()
    if os.path.realpath(cwd) != os.path.realpath(root) and not os.path.commonpath(
        (os.path.realpath(cwd), os.path.realpath(root))
    ) == os.path.realpath(root):
        raise VerifierUnavailable("Lean sandbox working directory escaped its configured root")
    if not command:
        raise VerifierUnavailable("Lean sandbox received an empty compiler command")
    sandbox_command = [_lean_sandbox_executable()]
    sandbox_command.extend(_sandbox_lean_argument(item, cwd) for item in command[1:])
    result = isolated_runner.run_isolated(
        image,
        sandbox_command,
        cwd,
        writable_workspace=True,
        timeout=max(1, min(120, int(math.ceil(timeout)))),
        memory_mb=1024,
        cpus=1,
        pids=64,
        output_bytes=isolated_runner.MAX_OUTPUT_BYTES,
    )
    output = (result.get("stdout") or "") + (result.get("stderr") or "")
    if result.get("runtime") == "" and not result.get("ok"):
        raise VerifierUnavailable(result.get("error") or "Lean sandbox is unavailable")
    if result.get("error"):
        raise VerifierUnavailable("Lean sandbox failed: %s" % result["error"])
    returncode = result.get("returncode")
    if type(returncode) is not int:
        raise VerifierUnavailable("Lean sandbox returned no compiler exit code")
    return returncode, output


def lean_check(artifact, spec=None):
    """Kernel-check Lean 4 source without accepting proof placeholders.

    ``spec={'lean': executable?, 'lake': executable?, 'project': directory?,
    'timeout': seconds?, 'expected_declaration': name?, 'expected_type': type?,
    'trusted_prelude': caller-owned Lean source?}``.
    The expected declaration/type pair is compiled into a separate trusted
    module after the artifact compiler process exits. A separate auditor
    compares the resulting kernel types and checks axiom dependencies, so
    submitted syntax/macros cannot rewrite the contract check. Contract-local
    definitions may be put in
    ``trusted_prelude``; that caller-owned source is compiled separately and
    imported by both sides without exposing the contract axiom to the artifact.
    With a project, the proof runs through ``lake env lean`` so pinned
    dependencies such as Mathlib are available. The
    defaults may be supplied through ``SONDER_LEAN_EXE``,
    ``SONDER_LAKE_EXE``, and ``SONDER_LEAN_PROJECT``. Without an explicit Lean
    executable or project, the repository ``lean-toolchain`` pin is applied and
    its exact version is verified. Executables are identity-probed before use.
    Missing Lean/Lake is ``VerifierUnavailable``; rejected source or a kernel
    diagnostic is an ordinary failed verdict. Network/package installation is
    never attempted.
    """
    if not isinstance(artifact, str) or not artifact.strip():
        raise ValueError("Lean source must be a non-empty string")
    if len(artifact.encode("utf-8")) > _MAX_LEAN_SOURCE_BYTES:
        raise ValueError("Lean source exceeds the 256000-byte verifier ceiling")
    spec = dict(spec or {})
    unknown = set(spec) - {
        "lean", "lake", "project", "timeout",
        "expected_declaration", "expected_type", "trusted_prelude",
    }
    if unknown:
        raise ValueError("unsupported lean_check options: %s" % sorted(unknown))
    timeout = spec.get("timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("lean_check timeout must be numeric")
    timeout = float(timeout)
    if not 1 <= timeout <= 300:
        raise ValueError("lean_check timeout must be within [1, 300] seconds")
    contract = lean_contract(spec)
    trusted_prelude = lean_trusted_prelude(spec)
    if trusted_prelude is not None and contract is None:
        raise ValueError(
            "trusted_prelude requires expected_declaration and expected_type"
        )

    gap = _LEAN_TRUST_GAP_RE.search(_lean_code_only(artifact))
    if gap:
        detail = "Lean source contains prohibited unproved trust gap: %s" % gap.group(1)
        return Verdict(False, "unproved trust gap", detail)

    # Model-authored Lean is executable metaprogram code.  Never select a host
    # executable or project directory for it: the configured OCI image owns the
    # complete Lean toolchain and any approved libraries.
    _lean_sandbox_image()
    project = _lean_project(spec.get("project"))
    if project is not None:
        raise VerifierUnavailable(
            "Lean sandbox images must contain approved dependencies; host projects are not mounted"
        )
    configured_lean = spec.get("lean")
    environment_lean = os.environ.get("SONDER_LEAN_EXE", "").strip()
    use_repository_pin = not configured_lean and not environment_lean
    # Legacy host executable settings are intentionally not executed.  The
    # image-local executable is the only compiler authority for this path.
    command = [_lean_sandbox_executable()]

    directory = tempfile.mkdtemp(prefix="sonder-lean-", dir=_lean_sandbox_root())
    try:
        expected_version = None
        if use_repository_pin:
            pin, expected_version = _repository_lean_toolchain()
            with open(
                os.path.join(directory, "lean-toolchain"), "w", encoding="utf-8"
            ) as handle:
                handle.write(pin + "\n")
        try:
            version_rc, version_output = _run_lean_in_sandbox(
                [*command, "--version"],
                cwd=directory,
                timeout=min(timeout, 30),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VerifierUnavailable(
                "Lean 4 identity probe failed: %s" % type(exc).__name__
            ) from exc
        version_match = _LEAN_VERSION_RE.search(version_output)
        if version_rc != 0 or version_match is None:
            raise VerifierUnavailable(
                "configured lean executable failed its identity probe"
            )
        if expected_version and version_match.group(1) != expected_version:
            raise VerifierUnavailable(
                "default Lean version %s does not match repository pin %s"
                % (version_match.group(1), expected_version)
            )

        output = ""
        lean_path = directory
        if os.environ.get("LEAN_PATH"):
            lean_path += os.pathsep + os.environ["LEAN_PATH"]
        module_env = {"LEAN_PATH": lean_path}
        prelude_module = None
        prelude_path = None
        prelude_source = None
        prelude_compile_command = None
        if trusted_prelude is not None:
            prelude_module = "SonderTrustedPrelude_%s" % secrets.token_hex(12)
            prelude_path = os.path.join(directory, prelude_module + ".lean")
            prelude_source = _lean_trusted_prelude_source(
                artifact, trusted_prelude,
            )
            with open(prelude_path, "w", encoding="utf-8") as handle:
                handle.write(prelude_source)
            prelude_compile_command = [
                *command,
                "-R", directory,
                "-o", os.path.join(directory, prelude_module + ".olean"),
                prelude_path,
            ]
            try:
                prelude_rc, prelude_output = _run_lean_in_sandbox(
                    prelude_compile_command,
                    cwd=directory,
                    timeout=timeout,
                    env_overrides=module_env,
                )
            except FileNotFoundError as exc:
                raise VerifierUnavailable(
                    "Lean 4 executable disappeared before checking the trusted prelude"
                ) from exc
            output += prelude_output
            if prelude_rc != 0:
                detail = output[-_MAX_LEAN_DETAIL_CHARS:]
                return Verdict(
                    False,
                    _last_line(output) or "Lean trusted prelude failed",
                    detail,
                )

        path = os.path.join(directory, "Main.lean")
        submitted_source = artifact
        if prelude_module:
            submitted_source = "import %s\n\n%s" % (prelude_module, artifact)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(submitted_source)
        compile_command = [*command, path]
        if contract is not None:
            compile_command = [
                *command,
                "-R", directory,
                "-o", os.path.join(directory, "Main.olean"),
                path,
            ]
        try:
            rc, compile_output = _run_lean_in_sandbox(
                compile_command, cwd=directory, timeout=timeout,
                env_overrides=module_env,
            )
        except FileNotFoundError as exc:
            raise VerifierUnavailable("Lean 4 executable disappeared before checking") from exc
        output += compile_output
        if rc == 0 and contract is not None:
            # The artifact can execute metaprogram commands while it compiles.
            # Create the randomized contract only after that process exits, and
            # restore the caller-owned prelude from memory before trusting it.
            if prelude_compile_command is not None:
                with open(prelude_path, "w", encoding="utf-8") as handle:
                    handle.write(prelude_source)
                try:
                    restored_rc, restored_output = _run_lean_in_sandbox(
                        prelude_compile_command,
                        cwd=directory,
                        timeout=timeout,
                        env_overrides=module_env,
                    )
                except FileNotFoundError as exc:
                    raise VerifierUnavailable(
                        "Lean 4 executable disappeared while restoring the trusted prelude"
                    ) from exc
                output += restored_output
                if restored_rc != 0:
                    raise VerifierUnavailable(
                        "trusted Lean prelude could not be restored after artifact compilation: %s"
                        % (_last_line(restored_output) or "unknown compiler failure")
                    )

            expected_module = "SonderExpectedContract_%s" % secrets.token_hex(12)
            expected_declaration = "%s.contract" % expected_module
            expected_path = os.path.join(directory, expected_module + ".lean")
            with open(expected_path, "w", encoding="utf-8") as handle:
                handle.write(_lean_expected_contract_source(
                    artifact, contract[1], expected_module, prelude_module,
                ))
            try:
                expected_rc, expected_output = _run_lean_in_sandbox(
                    [
                        *command,
                        "-R", directory,
                        "-o", os.path.join(directory, expected_module + ".olean"),
                        expected_path,
                    ],
                    cwd=directory,
                    timeout=timeout,
                    env_overrides=module_env,
                )
            except FileNotFoundError as exc:
                raise VerifierUnavailable(
                    "Lean 4 executable disappeared before checking the contract"
                ) from exc
            output += expected_output
            if expected_rc != 0:
                detail = output[-_MAX_LEAN_DETAIL_CHARS:]
                return Verdict(
                    False,
                    _last_line(output) or "Lean theorem contract type failed",
                    detail,
                )

            auditor_path = os.path.join(directory, "SonderAxiomAudit.lean")
            with open(auditor_path, "w", encoding="utf-8") as handle:
                handle.write(_LEAN_AXIOM_AUDITOR_SOURCE)
            try:
                audit_rc, audit_output = _run_lean_in_sandbox(
                    [
                        *command,
                        "-R", directory,
                        "--run", auditor_path,
                        "Main", contract[0],
                        expected_module, expected_declaration,
                    ],
                    cwd=directory,
                    timeout=timeout,
                    env_overrides=module_env,
                )
            except FileNotFoundError as exc:
                raise VerifierUnavailable(
                    "Lean 4 executable disappeared before the axiom audit"
                ) from exc
            if audit_rc != 0:
                audit_detail = audit_output[-_MAX_LEAN_DETAIL_CHARS:]
                if _LEAN_AXIOM_AUDIT_REJECTION in audit_output:
                    return Verdict(
                        False, "unproved axiom dependency", audit_detail,
                    )
                if _LEAN_CONTRACT_AUDIT_REJECTION in audit_output:
                    return Verdict(
                        False, "theorem contract mismatch", audit_detail,
                    )
                raise VerifierUnavailable(
                    "Lean axiom dependency audit failed: %s"
                    % (_last_line(audit_output) or "unknown audit failure")
                )
            output += audit_output
        detail = output[-_MAX_LEAN_DETAIL_CHARS:]
        return Verdict(
            rc == 0,
            "checked" if rc == 0 else (_last_line(output) or "Lean kernel check failed"),
            detail,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --- llm_judge: model-graded rubric for non-executable outputs -------------
def llm_judge(artifact, spec=None):
    """spec={'rubric': str, 'threshold': int 0-10, 'judge_fn': callable?}. Weak
    oracle for outputs with no executable check (design, prose). judge_fn(prompt)
    -> text is injectable; defaults to the local model selected by Sonder Runtime."""
    import re
    spec = spec or {}
    rubric = spec.get("rubric", "Is this a correct, complete, high-quality answer?")
    threshold = spec.get("threshold", 7)
    judge_fn = spec.get("judge_fn")
    if judge_fn is None:
        import server
        model = server.resolve_sonder_model(False)
        judge_fn = server._make_generate(
            model, "You are a strict grader. Reply with one integer 0-10, then a brief reason.",
            0.0, 256, 4096)
    resp = judge_fn("RUBRIC: %s\n\nOUTPUT TO GRADE:\n%s\n\nScore 0-10 (integer first):"
                    % (rubric, artifact)) or ""
    m = re.search(r"\d+", resp)
    score = int(m.group()) if m else 0
    return Verdict(score >= threshold, "judge %d/%d" % (score, threshold), resp)


REGISTRY = {
    "python_exec": python_exec,
    "program_run": program_run,
    "pytest_run": pytest_run,
    "typecheck": typecheck,
    "cpp_compile": cpp_compile,
    "lean_check": lean_check,
    "llm_judge": llm_judge,
}


def get(name):
    if name not in REGISTRY:
        raise KeyError("no verifier %r (have %s; planned %s)"
                       % (name, sorted(REGISTRY), sorted(PLANNED)))
    return REGISTRY[name]


def verify(name, artifact, spec=None):
    """The single seam solver/ladder/reward call. Adding a domain never touches them."""
    return get(name)(artifact, spec)


# External verifier backends promoted from the improvement fleet. Registered
# defensively — a missing/broken ext module never breaks the core registry.
#
# Their Verdict compatibility is STRUCTURAL, not by shared class: node_verifier
# does `from verifiers import Verdict, VerifierUnavailable`, but sql_verifier and
# json_schema_verifier each define their own Verdict namedtuple, so
# `isinstance(v, verifiers.Verdict)` is False for those two. That is fine — the
# only Verdict surface solver/ladder/reward touch is the .passed/.reason/.detail
# fields, which every backend has.
#
# The exception class is NOT interchangeable that way. A backend that signals
# "could not judge" MUST raise THIS module's VerifierUnavailable, or an
# `except verifiers.VerifierUnavailable` around verify() silently misses it —
# a same-named local subclass of RuntimeError is a different type. sql_verifier
# and json_schema_verifier have no external tool and never raise it at all.
# test_verifiers.py pins that rule for every promoted backend.
def _register_ext(mod_name, fn_name):
    """Resolve a promoted backend's entry point, tolerating the circular-import
    window. A backend that imports from `verifiers` at module scope (node_verifier
    does) is only half-initialized while THIS module executes, so `fn_name` may
    not be bound on it yet and an eager getattr silently drops the backend —
    `import node_verifier` before `import verifiers` used to leave "node_run"
    missing from REGISTRY entirely. Bind a late-resolving shim in exactly that
    window; a genuinely absent or broken module still raises out and stays
    unregistered, so the core registry is unaffected either way."""
    mod = __import__(mod_name)
    fn = getattr(mod, fn_name, None)
    if fn is not None:
        return fn
    if not getattr(getattr(mod, "__spec__", None), "_initializing", False):
        raise AttributeError("module %r has no %r" % (mod_name, fn_name))

    def _late(artifact, spec=None):
        return getattr(sys.modules[mod_name], fn_name)(artifact, spec)

    _late.__name__ = fn_name
    _late.__doc__ = "late-bound %s.%s (mid circular import; resolved on first call)" % (
        mod_name, fn_name)
    return _late


for _key, _mod, _fn in (
    ("node_run", "node_verifier", "node_run"),
    ("sql_valid", "sql_verifier", "sql_valid"),
    ("json_schema", "json_schema_verifier", "json_schema_verify"),
    ("ruff_check", "ruff_verifier", "ruff_check"),
):
    try:
        REGISTRY[_key] = _register_ext(_mod, _fn)
    except Exception:
        pass
