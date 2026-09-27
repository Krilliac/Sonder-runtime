"""Plan a structured test run: detect the runner, resolve the executable,
and build the host-owned argv.

``project_detect`` reports the test commands a project declares; this planner
maps each to a runner template and never executes (or even keeps) the argv it
detected. The executable comes from the host tool inventory, a project
virtualenv interpreter, or -- on POSIX only, when the host lacks the tool -- a
project's gradle/maven wrapper; the last two run project code and say so in
the plan notes (the run is execution-graded either way).
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Callable, Mapping

from ...application.context import OperationContext
from ...application.testing.ports import TestRunPlan, TestRunRequest
from ...domain.common.errors import InvalidInput
from ...domain.testing.runners import (
    MAX_WORKERS,
    RUNNER_ORDER,
    RUNNER_TEMPLATES,
    WORKERS_UNSUPPORTED,
    ReportFormat,
    RunnerTemplate,
    TestRunner,
    batch_safe,
    build_argv,
    clamp_timeout,
    command_digest,
    ctest_template,
    js_template,
    parse_version_tuple,
    runner_from_name,
    without_report,
)
from ...domain.testing.selectors import (
    PATH_KINDS,
    SELECTOR_ESCAPES_PROJECT,
    SelectorRejected,
    TestSelector,
    parse_selector,
)
from ..filesystem import file_ops
from ..inspection.project_detect import detect_project

NO_RUNNER_DETECTED = "NO_RUNNER_DETECTED"
RUNNER_UNAVAILABLE = "RUNNER_UNAVAILABLE"
CTEST_BUILD_TREE_MISSING = "CTEST_BUILD_TREE_MISSING"
PROJECT_OUTSIDE_ROOTS = "PROJECT_OUTSIDE_ROOTS"
INVALID_RUNNER = "INVALID_RUNNER"
BATCH_ARGUMENT_UNSAFE = "batch_argument_unsafe"

DEFAULT_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
MAX_PACKAGE_JSON_BYTES = 256 * 1024
MAX_BUILD_DIR_ENTRIES = 256
REPORT_ROOT_NAME = "test-runs"

# Removed from the inherited (already secret-scrubbed) environment: they can
# inject plugins/options into the runner or redirect git inside tests.
REMOVED_ENVIRONMENT = frozenset({
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_ASKPASS", "SSH_ASKPASS",
})
PINNED_ENVIRONMENT = (
    ("CI", "1"), ("NO_COLOR", "1"), ("FORCE_COLOR", "0"), ("CLICOLOR", "0"),
    ("TERM", "dumb"), ("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONIOENCODING", "utf-8"),
    ("PYTHONUNBUFFERED", "1"), ("CARGO_TERM_COLOR", "never"),
    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"), ("DOTNET_NOLOGO", "1"),
    ("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1"), ("GIT_TERMINAL_PROMPT", "0"),
)
_VENV_NAMES = (".venv", "venv", "env")
_PY_TEST_FILE = re.compile(r"^test.*\.py$")
_JS_RUNNERS = {"npm": TestRunner.NPM, "pnpm": TestRunner.PNPM, "yarn": TestRunner.YARN}
_BATCH_SUFFIXES = (".bat", ".cmd")


def _error(code: str, message: str) -> InvalidInput:
    error = InvalidInput(message)
    error.code = code
    return error


def test_environment(base: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """The replacement environment a test run launches with."""
    from ...platform.logging import child_environment

    environment = child_environment(base)
    for key in list(environment):
        if key.upper() in REMOVED_ENVIRONMENT:
            environment.pop(key, None)
    environment.update(PINNED_ENVIRONMENT)
    return tuple(sorted(environment.items()))


test_environment.__test__ = False  # not a pytest test function


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


class ProjectTestPlanner:
    """``TestRunPlanner`` over ``project_detect`` and the host tool inventory."""

    def __init__(self, lookup, *, state_dir: str, redact: Callable[[str], str],
                 system: str | None = None,
                 environment: Callable[[], tuple[tuple[str, str], ...]] = test_environment,
                 memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
                 cpu_count: Callable[[], int | None] = os.cpu_count) -> None:
        self._lookup = lookup
        self._state_dir = Path(state_dir)
        self._redact = redact
        self._system = system or ("Windows" if os.name == "nt" else "posix")
        self._environment = environment
        self._memory_limit = memory_limit_bytes
        self._cpu_count = cpu_count

    @property
    def report_root(self) -> Path:
        return self._state_dir / REPORT_ROOT_NAME

    # -- public ----------------------------------------------------------------

    def plan(self, request: TestRunRequest, context: OperationContext) -> TestRunPlan:
        runner_name = str(request.runner or "auto")
        if runner_name != "auto":
            try:
                requested = runner_from_name(runner_name)
            except InvalidInput:
                raise _error(INVALID_RUNNER, "unknown test runner") from None
        else:
            requested = None
        workers = request.workers
        if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                    or not 1 <= workers <= MAX_WORKERS):
            raise _error(WORKERS_UNSUPPORTED, "workers must be an integer within 1..%d" % MAX_WORKERS)
        if workers is not None:
            workers = min(workers, max(1, min(MAX_WORKERS, self._cpu_count() or 1)))
        root = self._project_root(str(request.project or "."), context)
        candidates = self._candidates(root, requested)
        if not candidates:
            if requested is not None:
                raise _error(NO_RUNNER_DETECTED,
                             "the project declares no %s tests" % requested.value)
            raise _error(NO_RUNNER_DETECTED, "no supported test runner detected in the project")
        pool = [item for item in candidates if requested is None or item[0] is requested]
        if not pool:
            raise _error(NO_RUNNER_DETECTED, "the project declares no %s tests" % requested.value)
        pool.sort(key=lambda item: (0 if item[1] == "." else item[1].count("/") + 1,
                                    RUNNER_ORDER.index(item[0]), item[1]))
        runner, cwd_rel, detected = pool[0]
        listed = tuple(dict.fromkeys("%s@%s" % (item[0].value, item[1]) for item in candidates))[:16]
        return self._build(root, runner, cwd_rel, detected, request, workers, listed[:16])

    # -- root and detection ----------------------------------------------------

    def _project_root(self, project: str, context: OperationContext) -> Path:
        if not project.strip() or "\x00" in project or len(project) > 1024:
            raise _error(PROJECT_OUTSIDE_ROOTS, "project must be a non-empty path")
        try:
            root = file_ops.resolve_repository_read_path(
                project, allow_workspace_root=True, reject_sensitive=True)
        except (PermissionError, ValueError) as exc:
            raise _error(PROJECT_OUTSIDE_ROOTS, "project is outside the authorized roots: %s"
                         % self._redact(str(exc))) from None
        requested = Path(project).expanduser()
        if not requested.is_absolute():
            requested = file_ops.workspace_root() / requested
        lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(requested))))
        physical = os.path.normcase(os.path.normpath(os.path.realpath(str(requested))))
        if lexical != physical or _is_reparse(Path(lexical)):
            raise _error(PROJECT_OUTSIDE_ROOTS, "project path traverses a symlink or junction")
        if not root.is_dir():
            raise _error(PROJECT_OUTSIDE_ROOTS, "project must be an existing directory")
        grants = tuple(Path(item).resolve() for item in (context.workspace_roots or ()))
        if grants and not any(_inside(root, grant) for grant in grants):
            raise _error(PROJECT_OUTSIDE_ROOTS, "project is outside this caller's workspace grant")
        return root

    def _candidates(self, root: Path, requested: TestRunner | None):
        try:
            detection = detect_project(str(root), max_depth=4, max_files=200)
        except (OSError, PermissionError, ValueError) as exc:
            raise _error(PROJECT_OUTSIDE_ROOTS, "project detection refused the path: %s"
                         % self._redact(str(exc))) from None
        found: list[tuple[TestRunner, str, list[str]]] = []
        for command in detection.get("commands", ()):
            if command.get("kind") != "test":
                continue
            argv = [str(item) for item in command.get("argv") or ()]
            cwd = str(command.get("cwd") or ".")
            if ".." in cwd.split("/"):
                continue
            runner = self._map(argv)
            if runner is not None:
                found.append((runner, cwd, argv))
        python_evidence = self._python_evidence(root, detection)
        if requested is TestRunner.UNITTEST and python_evidence:
            found.append((TestRunner.UNITTEST, ".", []))
        if requested is TestRunner.PYTEST and python_evidence and not any(
                item[0] is TestRunner.PYTEST for item in found):
            found.append((TestRunner.PYTEST, ".", []))
        return found

    @staticmethod
    def _map(argv: list[str]) -> TestRunner | None:
        if argv[:3] == ["python", "-m", "pytest"]:
            return TestRunner.PYTEST
        if argv[:2] == ["cargo", "test"]:
            return TestRunner.CARGO
        if argv[:2] == ["go", "test"]:
            return TestRunner.GO
        if argv[:1] == ["ctest"]:
            return TestRunner.CTEST
        if argv[:2] == ["dotnet", "test"] and len(argv) == 3:
            return TestRunner.DOTNET
        if argv == ["mvn", "test"]:
            return TestRunner.MAVEN
        if argv in (["gradle", "test"], ["./gradlew", "test"], ["gradlew.bat", "test"]):
            return TestRunner.GRADLE
        if argv == ["make", "test"]:
            return TestRunner.MAKE
        if argv in (["npm", "run", "test"], ["pnpm", "run", "test"], ["yarn", "test"]):
            return _JS_RUNNERS[argv[0]]
        return None

    @staticmethod
    def _python_evidence(root: Path, detection: Mapping) -> bool:
        if any(str(item.get("type", "")).startswith(("python", "pytest")) and "/" not in str(item.get("path", ""))
               for item in detection.get("manifests", ())):
            return True
        tests_dir = root / "tests"
        if tests_dir.is_dir() and not _is_reparse(tests_dir):
            return True
        try:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index > 2048:
                        break
                    if _PY_TEST_FILE.match(entry.name) and entry.is_file(follow_symlinks=False):
                        return True
        except OSError:
            return False
        return False

    # -- plan assembly ---------------------------------------------------------

    def _cwd(self, root: Path, cwd_rel: str) -> Path:
        cwd = (root / cwd_rel) if cwd_rel not in {"", "."} else root
        current = cwd
        while current != root:
            if _is_reparse(current):
                raise _error(PROJECT_OUTSIDE_ROOTS, "test directory traverses a symlink")
            current = current.parent
        resolved = cwd.resolve()
        if not _inside(resolved, root) or not resolved.is_dir():
            raise _error(PROJECT_OUTSIDE_ROOTS, "test directory escapes the project")
        return resolved

    def _tool(self, name: str) -> str | None:
        record = self._lookup.lookup(name)
        return None if record is None else str(record.path)

    def _tool_version(self, name: str) -> tuple[int, ...] | None:
        record = self._lookup.lookup(name)
        return None if record is None else parse_version_tuple(getattr(record, "version", ""))

    def _venv_python(self, root: Path, cwd: Path) -> Path | None:
        relative = "Scripts/python.exe" if self._system == "Windows" else "bin/python"
        for base in dict.fromkeys((cwd, root)):
            for name in _VENV_NAMES:
                venv = base / name
                if _is_reparse(venv) or not (venv / "pyvenv.cfg").is_file():
                    continue
                interpreter = venv / relative
                if not os.path.lexists(interpreter):
                    continue
                if _is_reparse(interpreter.parent):
                    continue
                if _regular_file(interpreter.resolve()):
                    return interpreter
        return None

    @staticmethod
    def _xdist_available(interpreter: Path) -> bool:
        prefixes = [interpreter.parent.parent]
        try:
            prefixes.append(interpreter.resolve().parent.parent)
        except OSError:
            pass
        for prefix in dict.fromkeys(prefixes):
            candidates = [prefix / "Lib" / "site-packages" / "xdist",
                          prefix / "lib" / "python3" / "dist-packages" / "xdist"]
            lib = prefix / "lib"
            try:
                with os.scandir(lib) as entries:
                    for index, entry in enumerate(entries):
                        if index > 64:
                            break
                        if entry.name.startswith("python3"):
                            candidates.append(Path(entry.path) / "site-packages" / "xdist")
                            candidates.append(Path(entry.path) / "dist-packages" / "xdist")
            except OSError:
                pass
            if any((item / "__init__.py").is_file() for item in candidates):
                return True
        return False

    def _ctest_build_dir(self, cwd: Path) -> str:
        options: list[str] = []
        try:
            with os.scandir(cwd) as entries:
                names = sorted(entry.name for index, entry in enumerate(entries)
                               if index < MAX_BUILD_DIR_ENTRIES)
        except OSError:
            names = []
        options.extend(name for name in names if name == "build")
        options.extend(name for name in names if name.startswith("build-"))
        out_build = cwd / "out" / "build"
        if out_build.is_dir() and not _is_reparse(cwd / "out") and not _is_reparse(out_build):
            try:
                with os.scandir(out_build) as entries:
                    options.extend(sorted("out/build/" + entry.name for index, entry in enumerate(entries)
                                          if index < MAX_BUILD_DIR_ENTRIES))
            except OSError:
                pass
        for option in options:
            candidate = cwd / option
            parts = option.split("/")
            if any(_is_reparse(cwd.joinpath(*parts[: index + 1])) for index in range(len(parts))):
                continue
            if candidate.is_dir() and _regular_file(candidate / "CTestTestfile.cmake"):
                return option
        raise _error(CTEST_BUILD_TREE_MISSING,
                     "no configured CMake build tree (build/, build-*/, out/build/*) with "
                     "CTestTestfile.cmake; configure and build the project first")

    def _package_test_tool(self, cwd: Path) -> str:
        path = cwd / "package.json"
        try:
            guarded = file_ops.resolve_repository_read_path(
                str(path), allow_workspace_root=False, reject_sensitive=True)
            if _is_reparse(path) or not _regular_file(guarded):
                return ""
            with guarded.open("rb") as handle:
                raw = handle.read(MAX_PACKAGE_JSON_BYTES + 1)
        except (OSError, PermissionError, ValueError):
            return ""
        if len(raw) > MAX_PACKAGE_JSON_BYTES:
            return ""
        try:
            body = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return ""
        scripts = body.get("scripts") if isinstance(body, dict) else None
        script = scripts.get("test") if isinstance(scripts, dict) else None
        if not isinstance(script, str):
            return ""
        tokens = script.split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)  # leading VAR=value assignments
        if not tokens:
            return ""
        first = tokens[0].rsplit("/", 1)[-1]
        if first == "jest":
            return "jest"
        if first == "vitest" and (len(tokens) == 1 or tokens[1] == "run"):
            return "vitest"
        return ""

    def _selector(self, runner: TestRunner, raw: str, cwd: Path, root: Path) -> TestSelector | None:
        if not raw:
            return None
        selector = parse_selector(runner, raw)
        if selector.kind in PATH_KINDS and selector.path:
            target = cwd / selector.path
            try:
                resolved = Path(os.path.realpath(target))
            except OSError:
                resolved = target
            lexical = Path(os.path.normpath(os.path.abspath(target)))
            if (not _inside(lexical, root) or not _inside(resolved, root)
                    or not os.path.exists(resolved)):
                raise SelectorRejected(SELECTOR_ESCAPES_PROJECT,
                                       "selector path must exist inside the project")
        return selector

    def _executable(self, runner: TestRunner, template: RunnerTemplate, root: Path, cwd: Path,
                    notes: list[str]) -> tuple[str, str, tuple[str, ...], bool]:
        """(executable, interpreter_source, host-checked executables, project_executable)."""
        if runner in {TestRunner.PYTEST, TestRunner.UNITTEST}:
            venv = self._venv_python(root, cwd)
            if venv is not None:
                notes.append("interpreter: project virtualenv (runs project-installed packages)")
                return str(venv), "project_venv", (), True
            for name in ("python3", "python"):
                path = self._tool(name)
                if path:
                    return path, "inventory:" + name, (path,), False
            raise _error(RUNNER_UNAVAILABLE, "no host Python interpreter is available")
        path = self._tool(template.tool)
        if path:
            return path, "inventory:" + template.tool, (path,), False
        if runner in {TestRunner.GRADLE, TestRunner.MAVEN}:
            wrapper = "gradlew" if runner is TestRunner.GRADLE else "mvnw"
            if self._system == "Windows":
                raise _error(RUNNER_UNAVAILABLE,
                             "%s is not installed; Windows project wrappers are refused" % template.tool)
            candidate = cwd / wrapper
            if (os.path.lexists(candidate) and not _is_reparse(candidate)
                    and _regular_file(candidate) and _inside(candidate.resolve(), root)
                    and os.access(candidate, os.X_OK)):
                notes.append("executable: project %s wrapper (runs project code)" % wrapper)
                return str(candidate), "project_wrapper:" + wrapper, (), True
        raise _error(RUNNER_UNAVAILABLE, "%s is not available on this host" % template.tool)

    def _build(self, root: Path, runner: TestRunner, cwd_rel: str, detected: list[str],
               request: TestRunRequest, workers: int | None, candidates: tuple[str, ...]) -> TestRunPlan:
        notes: list[str] = []
        cwd = self._cwd(root, cwd_rel)
        template = RUNNER_TEMPLATES[runner]
        placeholders: dict[str, str] = {}
        if runner is TestRunner.CTEST:
            placeholders["build_dir"] = self._ctest_build_dir(cwd)
            template = ctest_template(self._tool_version("cmake") or self._tool_version("ctest"))
            if template.report_format is ReportFormat.TEXT_DIGEST:
                notes.append("ctest JUnit output needs CMake 3.21+; using the output digest")
        elif runner in {TestRunner.NPM, TestRunner.PNPM, TestRunner.YARN}:
            template = js_template(runner, self._package_test_tool(cwd))
        elif runner is TestRunner.DOTNET:
            project_file = detected[2] if len(detected) == 3 else ""
            # A project file name is project-controlled text that lands in argv:
            # it may not read as an option (``-p:...``, ``--...``).
            if not project_file or "/" in project_file or "\\" in project_file \
                    or project_file.startswith(("-", "@")) \
                    or not _regular_file(cwd / project_file):
                raise _error(NO_RUNNER_DETECTED, "the dotnet test project file is missing")
            placeholders["project_file"] = project_file
        executable, interpreter_source, checked, project_executable = self._executable(
            runner, template, root, cwd, notes)
        selector = self._selector(runner, str(request.selector or ""), cwd, root)
        if workers is not None and runner is TestRunner.PYTEST and not self._xdist_available(Path(executable)):
            raise _error(WORKERS_UNSUPPORTED, "pytest-xdist is not installed for the chosen interpreter")
        token = uuid.uuid4().hex
        report_dir = self.report_root / token
        if template.report_args or template.report_name:
            placeholders["report_dir"] = str(report_dir)
            placeholders["report"] = str(report_dir / (template.report_name or "report"))
        batch = self._system == "Windows" and executable.lower().endswith(_BATCH_SUFFIXES)
        argv = build_argv(template, executable=executable, selector=selector, workers=workers,
                          placeholders=placeholders)
        if batch and not batch_safe(argv[1:]):
            if template.report_args:
                template = without_report(template)
                for key in ("report", "report_dir"):
                    placeholders.pop(key, None)
                argv = build_argv(template, executable=executable, selector=selector,
                                  workers=workers, placeholders=placeholders)
                notes.append(BATCH_ARGUMENT_UNSAFE)
            if not batch_safe(argv[1:]):
                raise _error(BATCH_ARGUMENT_UNSAFE, "the command cannot be passed safely to a batch launcher")
        cwd_label = self._redact(str(cwd))
        digest = command_digest(argv, cwd_label, runner.value, placeholders)
        display = tuple(self._display(item, placeholders) for item in argv)
        timeout = clamp_timeout(template, request.timeout_seconds)
        return TestRunPlan(
            runner=runner,
            project_root=str(root),
            cwd=str(cwd),
            argv=argv,
            display_argv=display,
            cwd_label=cwd_label,
            command_digest=digest,
            report_format=template.report_format,
            report_dir=str(report_dir),
            timeout_seconds=timeout,
            max_descendants=template.max_descendants,
            memory_limit_bytes=self._memory_limit,
            environment=self._environment(),
            candidates=candidates,
            interpreter_source=interpreter_source,
            notes=tuple(notes),
            selector=selector.value if selector is not None else "",
            report_file=placeholders.get("report", ""),
            report_glob=template.report_glob,
            checked_executables=checked,
            project_executable=project_executable,
        )

    def _display(self, item: str, placeholders: Mapping[str, str]) -> str:
        text = str(item)
        for key in ("report", "report_dir"):
            value = placeholders.get(key)
            if value:
                text = text.replace(value, "{%s}" % key)
        return self._redact(text)


__all__ = [
    "BATCH_ARGUMENT_UNSAFE", "CTEST_BUILD_TREE_MISSING", "DEFAULT_MEMORY_LIMIT_BYTES",
    "INVALID_RUNNER", "NO_RUNNER_DETECTED", "PINNED_ENVIRONMENT", "PROJECT_OUTSIDE_ROOTS",
    "ProjectTestPlanner", "REMOVED_ENVIRONMENT", "REPORT_ROOT_NAME", "RUNNER_UNAVAILABLE",
    "test_environment",
]
