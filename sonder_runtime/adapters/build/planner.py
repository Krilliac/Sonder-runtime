"""Plan build models and build jobs: locate, validate, template, harden.

``ProjectBuildPlanner`` is the only place a build argv is assembled, and it
only ever renders the closed templates of ``domain.build.templates``:

* the project and build directory are resolved inside the caller's roots
  (the structured-test resolver), with no symlink or junction in the final
  components, and the build directory is never the project root or an
  ancestor of it;
* every value a model named (target, config, platform, preset, file) is
  checked by ``validate_request_against_model`` against the parsed model and
  its ``TargetSafety``: utility targets, custom targets and VS
  Makefile/Utility projects are refused unless the operator allowlisted them;
* the executable comes from the host inventory and passes
  ``require_host_executable`` here and again at launch; include traces run
  the inventory compiler, never the compile database's argv[0];
* the network decision (``unshare -rn`` or advisory, with the compiler-launcher
  downgrade) and the scrubbed environment (vcvars only for Ninja/NMake with
  ``cl``/``clang-cl`` and for MSVC include traces) are decided per plan;
* the approval digest covers the display argv (host paths redacted, private
  log paths as placeholders), cwd label, environment key names, world and
  network, so a re-plan of the same request digests identically.
"""
from __future__ import annotations

import os
import posixpath
import re
import stat
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ...application.build.ports import (
    ACTION_COMPILE_ONE,
    ACTION_CONFIGURE,
    ACTION_INCLUDE_TRACE,
    ACTION_UNSUPPORTED,
    BUILD_JOB_PREFIX,
    BUILD_TREE_MISSING,
    BUILD_TREE_REJECTED,
    PROJECT_OUTSIDE_ROOTS,
    RUNNER_UNAVAILABLE,
    UNKNOWN_FILE,
    UNKNOWN_PRESET,
    BuildJobPlan,
    BuildJobRequest,
    BuildModelRequest,
    BuildTreeLocation,
    RawBuildTree,
    build_error,
)
from ...application.context import OperationContext
from ...domain.build import compile_db as cdb
from ...domain.build import file_api
from ...domain.build import msbuild as msb
from ...domain.build import presets as presets_mod
from ...domain.build import templates as tpl
from ...domain.build.model import (
    ISOLATION_UNVERIFIED,
    VS_GENERATORS,
    BuildAction,
    BuildDomainError,
    BuildModel,
    BuildSystem,
    BuildWorld,
    CompileUnit,
    Generator,
    ModelSource,
    PchMode,
    PresetInfo,
    finalize_model,
    generator_from_name,
    path_label,
)
from ...domain.build.tool_targets import apply_safety, classify_targets
from .launcher import LOG_FILE_NAME, QUERY_RELATIVE
from .tree_reader import BUILD_LABEL, label_path

DEFAULT_MEMORY_LIMIT_BYTES: int | None = None
MSBUILD_LOG_NAME = "msbuild.log"
BINLOG_NAME = "build.binlog"
MAX_BUILD_CANDIDATES = 256
TRACE_COMPILERS = ("g++", "gcc", "clang++", "clang", "clang-cl", "cl", "c++", "cc")
KNOWN_LAUNCHER_TOOLS = ("ccache", "sccache", "buildcache")
_BUILD_DIR_NAME_RE = re.compile(r"^(?:build|build-[A-Za-z0-9_.-]{1,64}|cmake-build-[A-Za-z0-9_.-]{1,64}|out)$")
_LAUNCHER_KEYS = ("CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER", "CMAKE_CUDA_COMPILER_LAUNCHER")


def _error(code: str, message: str):
    return build_error(code, message)


def _domain_error(exc: BuildDomainError):
    return build_error(getattr(exc, "code", "") or "INVALID_INPUT", str(exc))


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _inside(child: str, parent: str) -> bool:
    child_n, parent_n = _norm(child), _norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


def _default_resolver(project: str, context: OperationContext) -> Path:
    from ..filesystem import file_ops

    return file_ops.resolve_repository_read_path(project, allow_workspace_root=True,
                                                 reject_sensitive=True)


def _workspace_root() -> Path:
    from ..filesystem import file_ops

    return file_ops.workspace_root()


class ProjectBuildPlanner:
    """``BuildPlanner`` over the tree reader, the host inventory and the domain templates."""

    def __init__(self, lookup, reader, environment, network, *, run_root: str,
                 redact: Callable[[str], str] = lambda text: text,
                 host: str | None = None,
                 clock: Callable[[], float] = time.time,
                 cpu_count: Callable[[], int | None] = os.cpu_count,
                 operator_max_timeout: int | None = None,
                 utility_allow: Iterable[str] = (),
                 profiles: Iterable[Any] = (),
                 operator_defines: Iterable[str] = (),
                 memory_limit_bytes: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
                 executable_guard: Callable[[str], str] | None = None,
                 resolve_root: Callable[[str, OperationContext], Path] = _default_resolver,
                 workspace_root: Callable[[], Path] = _workspace_root,
                 realpath: Callable[[str], str] = os.path.realpath) -> None:
        self._lookup = lookup
        self._reader = reader
        self._environment = environment
        self._network = network
        self._run_root = Path(run_root)
        self._redact = redact
        self._host = host or ("windows" if os.name == "nt" else
                              ("darwin" if os.uname().sysname == "Darwin" else "linux"))
        self._clock = clock
        self._cpu_count = cpu_count
        self._operator_max = operator_max_timeout
        self._utility_allow = frozenset(str(name) for name in utility_allow)
        self._profiles = {profile.name: profile for profile in profiles}
        self._defines = tuple(tpl.validate_define(item) for item in operator_defines)
        self._memory_limit = memory_limit_bytes
        if executable_guard is None:
            from ..host_tools.guards import require_host_executable as executable_guard
        self._guard = executable_guard
        self._resolve_root = resolve_root
        self._workspace_root = workspace_root
        self._realpath = realpath

    # -- location ----------------------------------------------------------------

    def _project_root(self, project: str, context: OperationContext) -> Path:
        project = str(project or ".")
        if not project.strip() or "\x00" in project or len(project) > 1024:
            raise _error(PROJECT_OUTSIDE_ROOTS, "project must be a non-empty path")
        try:
            root = Path(self._resolve_root(project, context))
        except (PermissionError, ValueError) as exc:
            raise _error(PROJECT_OUTSIDE_ROOTS, "project is outside the authorized roots: %s"
                         % self._redact(str(exc))) from None
        requested = Path(project).expanduser()
        if not requested.is_absolute():
            requested = self._workspace_root() / requested
        lexical = _norm(str(requested))
        physical = _norm(os.path.realpath(str(requested)))
        if lexical != physical or _is_reparse(Path(lexical)):
            raise _error(PROJECT_OUTSIDE_ROOTS, "project path traverses a symlink or junction")
        if not root.is_dir():
            raise _error(PROJECT_OUTSIDE_ROOTS, "project must be an existing directory")
        grants = tuple(Path(item).resolve() for item in (context.workspace_roots or ()))
        if grants and not any(_inside(str(root), str(grant)) for grant in grants):
            raise _error(PROJECT_OUTSIDE_ROOTS, "project is outside this caller's workspace grant")
        return root

    def _check_build_dir(self, root: Path, build_dir: Path, context: OperationContext) -> Path:
        candidate = Path(os.path.normpath(os.path.abspath(str(build_dir))))
        if _inside(str(root), str(candidate)):
            raise _error(BUILD_TREE_REJECTED,
                         "the build directory may not be the project root or its ancestor")
        if not _inside(str(candidate), str(root)):
            try:
                self._resolve_root(str(candidate), context)
            except (PermissionError, ValueError):
                raise _error(PROJECT_OUTSIDE_ROOTS,
                             "the build directory is outside the authorized roots") from None
            anchor = candidate
            while not anchor.exists() and anchor.parent != anchor:
                anchor = anchor.parent
            base = anchor
        else:
            base = root
        # No symlink or junction in any existing component below the base.
        current = candidate
        while current != base and current.parent != current:
            if _is_reparse(current):
                raise _error(BUILD_TREE_REJECTED, "the build directory traverses a symlink or junction")
            current = current.parent
        if os.path.exists(candidate) and _norm(os.path.realpath(candidate)) != _norm(str(candidate)):
            raise _error(BUILD_TREE_REJECTED, "the build directory resolves elsewhere")
        return candidate

    def _detect_build_dir(self, root: Path) -> Path | None:
        candidates: list[tuple[float, Path]] = []

        def consider(path: Path) -> None:
            if _is_reparse(path) or not path.is_dir():
                return
            markers = ("CMakeCache.txt", "compile_commands.json", "build.ninja")
            reply = path.joinpath(".cmake", "api", "v1", "reply")
            hit = [path / name for name in markers if (path / name).is_file()]
            if reply.is_dir():
                hit.append(reply)
            if not hit:
                return
            try:
                newest = max(item.stat().st_mtime for item in hit)
            except OSError:
                return
            candidates.append((newest, path))

        try:
            with os.scandir(root) as entries:
                names = sorted(entry.name for index, entry in enumerate(entries)
                               if index < 4096 and entry.is_dir(follow_symlinks=False))
        except OSError:
            names = []
        for name in names:
            if not _BUILD_DIR_NAME_RE.match(name):
                continue
            base = root / name
            consider(base)
            sub_parent = base / "build" if name == "out" else base
            try:
                with os.scandir(sub_parent) as entries:
                    subs = sorted(entry.name for index, entry in enumerate(entries)
                                  if index < MAX_BUILD_CANDIDATES and entry.is_dir(follow_symlinks=False))
            except OSError:
                subs = []
            if not _is_reparse(sub_parent):
                for sub in subs:
                    consider(sub_parent / sub)
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item[0], str(item[1])))[-1][1]

    def locate(self, request: BuildModelRequest, context: OperationContext) -> BuildTreeLocation:
        root = self._project_root(request.project, context)
        notes: list[str] = []
        build_dir: Path | None = None
        if request.build_dir:
            raw = str(request.build_dir)
            if "\x00" in raw or len(raw) > 1024:
                raise _error(BUILD_TREE_REJECTED, "build_dir must be a bounded path")
            requested = Path(raw).expanduser()
            build_dir = requested if requested.is_absolute() else root / requested
        elif request.preset:
            preset = self._preset(root, request.preset)
            build_dir = Path(preset.binary_dir)
        else:
            build_dir = self._detect_build_dir(root)
            if build_dir is not None:
                notes.append("build directory detected")
        checked = self._check_build_dir(root, build_dir, context) if build_dir is not None else None
        system = ""
        if (root / "CMakeLists.txt").is_file():
            system = "cmake"
        elif any(name.endswith(".sln") for name in os.listdir(root)[:4096]):
            system = "msbuild"
        build_text = str(checked) if checked is not None else ""
        label_root = self._redact(str(root))
        return BuildTreeLocation(
            project_root=str(root), build_dir=build_text,
            project_label=root.name or label_root, build_label=self._reader.build_label(str(root), build_text)
            if build_text else "", detected_system=system,
            build_dir_exists=bool(checked is not None and checked.is_dir()), notes=tuple(notes),
        )

    # -- presets -----------------------------------------------------------------

    def _preset_set(self, root: Path, raw: RawBuildTree | None = None):
        raw = raw if raw is not None else self._reader.read_presets(str(root))
        files = dict(raw.presets)
        main = files.get("CMakePresets.json")
        user = files.get("CMakeUserPresets.json")
        if main is None and user is None:
            return None
        host_system = {"windows": "Windows", "darwin": "Darwin"}.get(self._host, "Linux")
        try:
            return presets_mod.parse_cmake_presets(
                main if main is not None else b'{"version": 3}', includes=dict(raw.preset_includes),
                source_root=str(root), user_data=user, host_system=host_system)
        except BuildDomainError as exc:
            raise _domain_error(exc) from None

    def _preset(self, root: Path, name: str) -> PresetInfo:
        found = self._preset_set(root)
        preset = found.find(name, "configure") if found is not None else None
        if preset is None or preset.hidden:
            raise _error(UNKNOWN_PRESET, "preset is not known")
        if not preset.binary_dir_resolvable or not preset.binary_dir:
            raise _error(UNKNOWN_PRESET, "preset binaryDir depends on the environment")
        return preset

    # -- models ------------------------------------------------------------------

    def plan_model(self, request: BuildModelRequest, context: OperationContext, *,
                   location: BuildTreeLocation | None = None) -> BuildModel:
        location = location or self.locate(request, context)
        raw = self._reader.read(location.project_root, location.build_dir)
        return self.model_from_raw(raw, location)

    def model_from_raw(self, raw: RawBuildTree, location: BuildTreeLocation) -> BuildModel:
        root = location.project_root
        build_dir = location.build_dir
        preset_set = self._preset_set(Path(root), raw) if raw.presets else None
        presets = preset_set.all() if preset_set is not None else ()
        notes = list(raw.notes) + (list(preset_set.notes) if preset_set is not None else [])
        created = float(self._clock())
        try:
            if raw.reply_index and raw.reply_objects:
                name, data = raw.reply_index[0]
                index = file_api.parse_reply_index(data, name=name)
                model = file_api.model_from_file_api(
                    index=index, objects=dict(raw.reply_objects), source_root=root,
                    build_dir=build_dir, project_label=location.project_label, created_at=created,
                    unity_blobs=dict(raw.unity_blobs), presets=presets,
                    compile_db_available=raw.compile_db is not None, extra_notes=tuple(notes))
            elif raw.compile_db is not None:
                model = self._model_from_compile_db(raw, location, presets, notes, created)
            elif raw.solution is not None or raw.vcxproj:
                solution_label, solution_bytes = raw.solution if raw.solution else ("", None)
                model = msb.model_from_msbuild(
                    solution=solution_bytes, solution_label=solution_label,
                    projects=dict(raw.vcxproj), props=dict(raw.props_imports), source_root=root,
                    project_label=location.project_label, build_dir=build_dir, created_at=created)
                model = replace(model, presets=presets, notes=tuple(model.notes) + tuple(notes))
            elif raw.ninja_present or raw.makefile_present:
                system = BuildSystem.NINJA if raw.ninja_present else BuildSystem.MAKE
                source = ModelSource.NINJA_FILE if raw.ninja_present else ModelSource.MAKEFILE
                model = BuildModel(project_label=location.project_label, source_root=root,
                                   build_dir=build_dir, system=system, source=source,
                                   presets=presets, notes=tuple(notes) + (
                                       "no File API reply or compile database: no target model",),
                                   created_at=created)
            else:
                hint = ("configure it first (build_job action=configure%s)"
                        % (", preset=..." if presets else "")) if raw.cmake_lists_present else \
                    "no CMake, MSBuild, Ninja or Make build tree was found"
                raise _error(BUILD_TREE_MISSING, "no configured build tree: " + hint)
        except BuildDomainError as exc:
            raise _domain_error(exc) from None
        if raw.truncated and not model.truncated:
            model = replace(model, truncated=True)
        safety = classify_targets(model)
        model = replace(model, targets=apply_safety(model, safety))
        return finalize_model(model)

    def _model_from_compile_db(self, raw: RawBuildTree, location: BuildTreeLocation,
                               presets: tuple[PresetInfo, ...], notes: list[str],
                               created: float) -> BuildModel:
        database = cdb.parse_compile_commands(raw.compile_db or b"[]", source_root=location.project_root,
                                              build_dir=location.build_dir)
        units: list[CompileUnit] = []
        for entry in database.entries:
            if not entry.file_rel:
                continue
            flags = cdb.classify_flags(entry.argv)
            units.append(CompileUnit(
                file_label=entry.file_label, file_rel=entry.file_rel, family=flags.family,
                std=flags.std, define_count=flags.define_count,
                include_dir_count=flags.include_dir_count, pch=flags.pch,
                pch_header=self._rel_label(flags.pch_header, location),
                forced_includes=tuple(self._rel_label(item, location) for item in flags.forced_includes),
                flags_digest=flags.flags_digest))
        system = BuildSystem.NINJA if raw.ninja_present else (
            BuildSystem.MAKE if raw.makefile_present else BuildSystem.CMAKE)
        generator = generator_from_name(raw.cache_value("CMAKE_GENERATOR"))
        if raw.cache_value("CMAKE_GENERATOR"):
            system = BuildSystem.CMAKE
        configs = tuple(item for item in [raw.cache_value("CMAKE_BUILD_TYPE")] if item)
        return BuildModel(
            project_label=location.project_label, source_root=location.project_root,
            build_dir=location.build_dir, system=system, generator=generator, configs=configs,
            units=tuple(units), presets=presets, source=ModelSource.COMPILE_DB,
            compile_db_available=True, truncated=database.truncated,
            notes=tuple(notes) + tuple(database.notes), created_at=created)

    @staticmethod
    def _rel_label(path: str, location: BuildTreeLocation) -> str:
        if not path:
            return ""
        label, rel = path_label(path, source_root=location.project_root, build_dir=location.build_dir)
        return rel or label

    # -- runs --------------------------------------------------------------------

    def _tool(self, name: str) -> str:
        record = self._lookup.lookup(name)
        if record is None:
            raise _error(RUNNER_UNAVAILABLE, "%s is not available on this host" % name)
        path = str(record.path)
        try:
            self._guard(path)
        except PermissionError:
            raise _error(RUNNER_UNAVAILABLE, "%s failed the host executable guard" % name) from None
        return path

    def _source_model(self, location: BuildTreeLocation) -> BuildModel:
        """Presets-only model used to validate a first configure."""
        preset_set = self._preset_set(Path(location.project_root))
        return BuildModel(project_label=location.project_label, source_root=location.project_root,
                          build_dir=location.build_dir, system=BuildSystem.CMAKE,
                          presets=preset_set.all() if preset_set is not None else ())

    def plan_run(self, request: BuildJobRequest, model: BuildModel | None,
                 context: OperationContext, *, lease: str | None = None) -> BuildJobPlan:
        location = self.locate(request.model_request(), context)
        action = request.action
        if action != ACTION_CONFIGURE and model is None:
            raise _error(BUILD_TREE_MISSING, "no configured build tree; configure it first")
        if model is None or action == ACTION_CONFIGURE and not location.build_dir_exists:
            if location.detected_system != "cmake" and not request.profile:
                raise _error(ACTION_UNSUPPORTED, "configure needs a CMake project (CMakeLists.txt)")
            model = model or self._source_model(location)
        safety = classify_targets(model)
        # A configure chooses CMAKE_BUILD_TYPE (syntax-checked by the template);
        # it names no existing config or target of the model.
        checked_request = replace(request, config="", target="", file="") \
            if action == ACTION_CONFIGURE else request
        try:
            validated = tpl.validate_request_against_model(
                checked_request, model, safety, operator_utility_allow=self._utility_allow)
        except BuildDomainError as exc:
            raise _domain_error(exc) from None
        notes: list[str] = list(validated.notes)
        token = uuid.uuid4().hex
        log_dir = self._run_root / (BUILD_JOB_PREFIX + token)
        log_file = str(log_dir / LOG_FILE_NAME)
        ctx = _PlanContext(location=location, model=model, request=request, validated=validated,
                           log_dir=str(log_dir), log_file=log_file, notes=notes)
        if request.profile:
            self._plan_profile(ctx)
        elif action == ACTION_CONFIGURE:
            self._plan_configure(ctx)
        elif action == ACTION_INCLUDE_TRACE:
            self._plan_trace(ctx)
        elif action == ACTION_COMPILE_ONE:
            self._plan_compile_one(ctx)
        else:
            self._plan_build(ctx)
        return self._finish(ctx, token, lease)

    # -- per action ----------------------------------------------------------------

    def _generator(self, model: BuildModel) -> Generator:
        return model.generator

    def _require_host_runner(self, model: BuildModel, ctx: "_PlanContext") -> None:
        generator = self._generator(model)
        if model.system is BuildSystem.MSBUILD or generator in VS_GENERATORS:
            if self._host != "windows":
                raise _error(RUNNER_UNAVAILABLE, "MSBuild requires Windows")
        if generator is Generator.XCODE:
            raise _error(ACTION_UNSUPPORTED, "Xcode generator trees are modelled but not built in v1")

    def _jobs(self, request: BuildJobRequest) -> str:
        return str(tpl.clamp_jobs(request.jobs, int(self._cpu_count() or 1)))

    def _plan_configure(self, ctx: "_PlanContext") -> None:
        request, validated, location = ctx.request, ctx.validated, ctx.location
        if validated.preset is not None:
            preset = validated.preset
            build_dir = self._check_build_dir(Path(location.project_root), Path(preset.binary_dir),
                                              _AnyContext())
            template = tpl.TEMPLATES["cmake.configure.preset"]
            values = {"preset": preset.name}
            ctx.generator = preset.generator
        else:
            build_dir = Path(location.build_dir) if location.build_dir else \
                Path(location.project_root) / "build"
            build_dir = self._check_build_dir(Path(location.project_root), build_dir, _AnyContext())
            generator = request.generator or self._default_generator()
            template = tpl.TEMPLATES["cmake.configure"]
            values = {"source_dir": location.project_root, "build_dir": str(build_dir),
                      "generator": generator}
            ctx.generator = generator
            if request.config and generator not in ("Ninja Multi-Config",) and \
                    not generator.startswith("Visual Studio"):
                values["config"] = request.config
            elif request.config:
                ctx.notes.append("config is chosen at build time for multi-config generators")
        if str(ctx.generator).startswith("Visual Studio") and self._host != "windows":
            raise _error(RUNNER_UNAVAILABLE, "Visual Studio generators require Windows")
        ctx.template = template
        ctx.values = values
        ctx.build_dir = str(build_dir)
        ctx.system = BuildSystem.CMAKE
        ctx.lists = {"net": tpl.network_hardening_args(BuildSystem.CMAKE, request.allow_network),
                     "defines": self._defines}
        ctx.cwd = location.project_root
        ctx.pre_writes = ((str(build_dir.joinpath(*QUERY_RELATIVE)), file_api.query_document()),)
        ctx.vcvars_family = self._configure_family(str(ctx.generator))

    def _default_generator(self) -> str:
        if self._lookup.lookup("ninja") is not None:
            return "Ninja"
        return "Visual Studio 17 2022" if self._host == "windows" else "Unix Makefiles"

    def _configure_family(self, generator: str) -> str:
        if self._host != "windows" or generator.startswith("Visual Studio"):
            return ""
        return "msvc" if generator in ("Ninja", "Ninja Multi-Config", "NMake Makefiles") else ""

    def _model_family(self, model: BuildModel) -> str:
        families = {toolchain.language: toolchain.family for toolchain in model.toolchains}
        family = families.get("CXX") or families.get("C") or ""
        if not family and model.units:
            family = model.units[0].family
        return family

    def _plan_build(self, ctx: "_PlanContext", *, target_override: str = "") -> None:
        model, request, validated, location = ctx.model, ctx.request, ctx.validated, ctx.location
        self._require_host_runner(model, ctx)
        target = target_override or validated.target
        config = validated.config
        if model.system is BuildSystem.MSBUILD:
            self._plan_msbuild_build(ctx, target)
            return
        if model.system is BuildSystem.NINJA:
            raise _error(ACTION_UNSUPPORTED,
                         "a bare Ninja tree has no build template; build it through CMake or a profile")
        if model.system is BuildSystem.MAKE:
            ctx.template = tpl.TEMPLATES["make.build"]
            ctx.values = {"build_dir": location.build_dir, "jobs": self._jobs(request)}
            if target:
                ctx.values["target"] = target
            ctx.system = BuildSystem.MAKE
            ctx.cwd = location.build_dir
            return
        if validated.build_preset is not None:
            ctx.template = tpl.TEMPLATES["cmake.build.preset"]
            ctx.values = {"build_preset": validated.build_preset.name, "jobs": self._jobs(request)}
            ctx.cwd = location.project_root
        else:
            ctx.template = tpl.TEMPLATES["cmake.build"]
            ctx.values = {"build_dir": location.build_dir, "jobs": self._jobs(request)}
            if model.multi_config:
                config = config or (model.configs[0] if model.configs else "")
                if config:
                    ctx.values["config"] = config
            ctx.cwd = location.build_dir
        if target:
            ctx.values["target"] = target
        ctx.config = config
        ctx.target = target
        ctx.system = BuildSystem.CMAKE
        generator = model.generator
        if self._host == "windows" and generator in (Generator.NINJA, Generator.NINJA_MULTI, Generator.NMAKE):
            ctx.vcvars_family = self._model_family(model) if self._model_family(model) in ("msvc", "clang_cl") else ""

    def _msbuild_defaults(self, model: BuildModel, validated) -> tuple[str, str]:
        config = validated.config or (model.configs[0] if model.configs else "")
        platform = validated.platform or (model.platforms[0] if model.platforms else "")
        if not config or not platform:
            raise _error(ACTION_UNSUPPORTED, "the solution declares no configuration or platform")
        return config, platform

    def _plan_msbuild_build(self, ctx: "_PlanContext", target: str) -> None:
        model, request, validated, location = ctx.model, ctx.request, ctx.validated, ctx.location
        config, platform = self._msbuild_defaults(model, validated)
        solution = self._solution_path(location)
        values = {"target": target or "Build", "config": config, "platform": platform,
                  "jobs": self._jobs(request), "log_file": str(Path(ctx.log_dir) / MSBUILD_LOG_NAME),
                  "binlog": str(Path(ctx.log_dir) / BINLOG_NAME)}
        if solution:
            values["solution"] = solution
        else:
            owner = validated.target_info
            if owner is None or not owner.project:
                raise _error(ACTION_UNSUPPORTED, "no solution: name a project target to build")
            values["project_file"] = self._project_path(location, owner.project)
        ctx.template = tpl.TEMPLATES["msbuild.build"]
        ctx.values = values
        ctx.lists = {"net": tpl.network_hardening_args(BuildSystem.MSBUILD, request.allow_network)}
        ctx.system = BuildSystem.MSBUILD
        ctx.cwd = location.project_root
        ctx.config, ctx.platform, ctx.target = config, platform, target or "Build"
        ctx.extra_logs = (values["log_file"],)
        ctx.binlog = values["binlog"]
        ctx.placeholders = {values["log_file"]: "{log_file}", values["binlog"]: "{binlog}"}
        ctx.notes.append("msbuild -m with cl /MP may oversubscribe; effective parallelism is "
                         "jobs x per-project /MP")

    def _solution_path(self, location: BuildTreeLocation) -> str:
        root = Path(location.project_root)
        for name in sorted(os.listdir(root))[:4096]:
            if name.endswith(".sln") and (root / name).is_file() and not _is_reparse(root / name):
                return str(root / name)
        return ""

    def _project_path(self, location: BuildTreeLocation, label: str) -> str:
        return label_path(location.project_root, location.build_dir, label)

    def _plan_compile_one(self, ctx: "_PlanContext") -> None:
        model, _request, validated, location = ctx.model, ctx.request, ctx.validated, ctx.location
        unit = validated.unit
        if unit is None:
            raise _error(UNKNOWN_FILE, "compile_one needs a file of the model")
        self._require_host_runner(model, ctx)
        ctx.file_label = unit.file_rel or unit.file_label
        fallback = validated.needs_target_build or unit.pch is PchMode.CREATE
        generator = model.generator
        if model.system is BuildSystem.MSBUILD or generator in VS_GENERATORS:
            self._plan_msbuild_compile(ctx, unit)
            return
        if generator not in (Generator.NINJA, Generator.NINJA_MULTI) and model.system is not BuildSystem.NINJA:
            fallback = True
            ctx.notes.append("this generator has no per-file build: verifying with a target build")
        if fallback:
            if not unit.target:
                raise _error(ACTION_UNSUPPORTED, "the file's target is unknown; build a target instead")
            self._plan_build(ctx, target_override=unit.target)
            ctx.template_note = "compile_one fell back to a target build"
            ctx.notes.append("compile_one of %s verified by building %s" % (ctx.file_label, unit.target))
            return
        if unit.unity_blob_rel:
            file_target = unit.unity_blob_rel + "^"
            ctx.notes.append("unity build: compiling the unity blob that includes this file")
        else:
            file_target = os.path.join(location.project_root, *unit.file_rel.split("/")) + "^"
        values = {"build_dir": location.build_dir, "file_target": file_target}
        config = validated.config or (unit.config if generator is Generator.NINJA_MULTI else "")
        if generator is Generator.NINJA_MULTI:
            config = config or (model.configs[0] if model.configs else "")
            if not config:
                raise _error(ACTION_UNSUPPORTED, "multi-config tree without configs")
            values["config"] = config
        ctx.template = tpl.TEMPLATES["ninja.compile_one"]
        ctx.values = values
        ctx.system = BuildSystem.NINJA
        ctx.cwd = location.build_dir
        ctx.config = config
        ctx.target = unit.target
        if self._host == "windows" and unit.family in ("msvc", "clang_cl"):
            ctx.vcvars_family = unit.family

    def _plan_msbuild_compile(self, ctx: "_PlanContext", unit: CompileUnit) -> None:
        model, validated, location = ctx.model, ctx.validated, ctx.location
        config, platform = (self._msbuild_defaults(model, validated) if model.system is BuildSystem.MSBUILD
                            else (validated.config or (model.configs[0] if model.configs else ""),
                                  validated.platform or "x64"))
        project = self._owning_project(ctx, unit)
        project_dir = os.path.dirname(project)
        source = os.path.join(location.project_root, *unit.file_rel.split("/"))
        selected = os.path.relpath(source, project_dir) if _inside(source, project_dir) else source
        selected = selected.replace("/", "\\")
        ctx.template = tpl.TEMPLATES["msbuild.compile_one"]
        ctx.values = {"project_file": project, "file_target": selected, "config": config,
                      "platform": platform}
        ctx.system = BuildSystem.MSBUILD
        ctx.cwd = os.path.dirname(project)
        ctx.config, ctx.platform, ctx.target = config, platform, unit.target
        if unit.pch is not PchMode.NONE:
            ctx.notes.append("precompiled header in use: a target build is the full verification")

    def _owning_project(self, ctx: "_PlanContext", unit: CompileUnit) -> str:
        model, location = ctx.model, ctx.location
        target = model.target(unit.target) if unit.target else None
        if model.system is BuildSystem.MSBUILD:
            if target is None or not target.project:
                raise _error(UNKNOWN_FILE, "the file's project is not in the solution model")
            return self._project_path(location, target.project)
        # CMake Visual Studio generator: the generated project named after the target.
        labels = self._reader.build_projects(location.build_dir)
        stem = unit.target
        for label in labels:
            if posixpath.splitext(posixpath.basename(label))[0] == stem:
                return label_path(location.project_root, location.build_dir, label)
        raise _error(RUNNER_UNAVAILABLE, "the generated project for target %s was not found" % stem)

    def _plan_trace(self, ctx: "_PlanContext") -> None:
        model, validated, location = ctx.model, ctx.validated, ctx.location
        unit = validated.unit
        if model.system is BuildSystem.MSBUILD or model.generator in VS_GENERATORS:
            raise _error(RUNNER_UNAVAILABLE,
                         "include_trace needs a compile database (Ninja or Makefile generators) in v1")
        if unit is None:
            raise _error(UNKNOWN_FILE, "include_trace needs a file of the model")
        data, _ = self._reader.read_compile_db(location.build_dir)
        if data is None:
            raise _error(RUNNER_UNAVAILABLE, "the compile database is missing or refused")
        try:
            database = cdb.parse_compile_commands(data, source_root=location.project_root,
                                                  build_dir=location.build_dir)
        except BuildDomainError as exc:
            raise _domain_error(exc) from None
        entry = database.entry_for(unit.file_rel)
        if entry is None:
            raise _error(UNKNOWN_FILE, "the file has no compile database entry")
        known = frozenset(name for name in KNOWN_LAUNCHER_TOOLS if self._lookup.lookup(name) is not None)
        rsp_contents: dict[str, str] = {}
        for token in entry.argv:
            if token.startswith("@"):
                text = self._reader.read_response_file(location.build_dir,
                                                       os.path.join(entry.directory, token[1:])
                                                       if not os.path.isabs(token[1:]) else token[1:])
                if text is not None:
                    rsp_contents[token[1:]] = text
        pch_header = ""
        if unit.pch_header:
            pch_header = os.path.join(location.project_root, *unit.pch_header.split("/")) \
                if not unit.pch_header.startswith("<") else ""
        result = cdb.sanitize_for_trace(
            entry.argv, unit.family, source_file=entry.file,
            roots=(location.project_root, location.build_dir), directory=entry.directory,
            pch_header=pch_header, known_launchers=known, rsp_contents=rsp_contents,
            windows=entry.windows)
        if isinstance(result, cdb.TraceRefused):
            raise _error(result.code, "include_trace refused: %s" % result.reason)
        compiler = self._trace_compiler(result.argv[0], entry.directory)
        argv = (compiler, *result.argv[1:])
        directory = entry.directory
        if not (_inside(directory, location.build_dir) or _inside(directory, location.project_root)):
            raise _error(BUILD_TREE_REJECTED, "the compile entry runs outside the project")
        ctx.raw_argv = argv
        ctx.template_id = "trace.msvc" if result.family in ("msvc", "clang_cl") else "trace.gnu"
        ctx.trace_family = "msvc" if result.family in ("msvc", "clang_cl") else "gnu"
        ctx.system = BuildSystem.CMAKE if model.system is BuildSystem.CMAKE else model.system
        ctx.cwd = directory
        ctx.file_label = unit.file_rel
        ctx.target = unit.target
        ctx.notes.extend(result.notes)
        ctx.checked_extra = (compiler,)
        forced: list[str] = []
        for index, token in enumerate(argv[:-1]):
            if token == "-include" and index + 1 < len(argv):
                forced.append(argv[index + 1])
            elif token.upper().startswith("/FI") and len(token) > 3:
                forced.append(token[3:])
        ctx.trace_forced = tuple(self._rel_label(item, location) for item in forced)
        if self._host == "windows" and result.family in ("msvc", "clang_cl"):
            ctx.vcvars_family = result.family

    def _trace_compiler(self, token: str, directory: str) -> str:
        """The inventory compiler whose realpath equals the database's compiler."""
        path = token if os.path.isabs(token) else ""
        if not path:
            base = token.replace("\\", "/").rsplit("/", 1)[-1]
            record = self._lookup.lookup(base)
            path = str(record.path) if record is not None else ""
        if not path:
            raise _error(RUNNER_UNAVAILABLE, "the trace compiler is not an inventory record")
        wanted = _norm(self._realpath(path))
        for name in TRACE_COMPILERS:
            record = self._lookup.lookup(name)
            if record is None:
                continue
            if _norm(self._realpath(str(record.path))) == wanted:
                try:
                    self._guard(str(record.path))
                except PermissionError:
                    continue
                return str(record.path)
        raise _error(RUNNER_UNAVAILABLE, "the trace compiler does not match an inventory compiler")

    def _plan_profile(self, ctx: "_PlanContext") -> None:
        request, location = ctx.request, ctx.location
        profile = self._profiles.get(request.profile)
        if profile is None:
            raise _error(ACTION_UNSUPPORTED, "unknown operator build profile")
        template = profile.template(BuildAction(request.action))
        if template is None:
            raise _error(ACTION_UNSUPPORTED, "the profile does not define %s" % request.action)
        values = {"source_dir": location.project_root, "jobs": self._jobs(request)}
        if location.build_dir:
            values["build_dir"] = location.build_dir
        for name in ("target", "config", "platform"):
            value = getattr(ctx.validated, name, "")
            if value:
                values[name] = value
        needed = template.placeholders()
        ctx.template = template
        ctx.values = {key: value for key, value in values.items() if key in needed}
        ctx.lists = {"defines": profile.defines} if "defines" in {
            name for segment in template.segments for token in segment.tokens
            for name in re.findall(r"\{([A-Za-z_]+)\}", token)} else {}
        ctx.system = BuildSystem.PROFILE
        ctx.cwd = location.build_dir if template.cwd == "build_dir" and location.build_dir else location.project_root
        ctx.executable_override = profile.executable
        ctx.profile_daemon = bool(profile.network_daemon)

    # -- assembly ----------------------------------------------------------------

    def _executable(self, ctx: "_PlanContext") -> str:
        override = getattr(ctx, "executable_override", "")
        if override:
            if os.path.isabs(override):
                try:
                    return self._guard(override)
                except PermissionError:
                    raise _error(RUNNER_UNAVAILABLE, "the profile executable failed the host guard") from None
            return self._tool(override)
        return self._tool(ctx.template.tool)

    def _finish(self, ctx: "_PlanContext", token: str, lease: str | None) -> BuildJobPlan:
        request, location, model = ctx.request, ctx.location, ctx.model
        if ctx.raw_argv:
            argv = tuple(ctx.raw_argv)
            template_id = ctx.template_id
            timeout = tpl.clamp_timeout(request.timeout_seconds, None, self._operator_max)
            max_descendants = tpl.MAX_DESCENDANTS
            checked = tuple(ctx.checked_extra)
        else:
            executable = self._executable(ctx)
            try:
                argv = tpl.build_argv(ctx.template, executable=executable, values=ctx.values,
                                      lists=ctx.lists)
            except BuildDomainError as exc:
                raise _domain_error(exc) from None
            template_id = ctx.template.template_id
            timeout = tpl.clamp_timeout(request.timeout_seconds, ctx.template, self._operator_max)
            max_descendants = ctx.template.max_descendants
            checked = (executable,)
        launchers = [value for key, value in self._reader.read_cache(ctx.build_dir or location.build_dir)
                     if key in _LAUNCHER_KEYS]
        if getattr(ctx, "profile_daemon", False):
            launchers.append("fbuild")
        decision = self._network.decide(allow_network=request.allow_network, launchers=launchers)
        environment = self._environment.environment(
            system=ctx.system.value, family=ctx.vcvars_family,
            toolchain_hint=self._toolset_hint(model), arch="x64")
        placeholders = dict(ctx.placeholders)
        display = tuple(self._display(item, placeholders, location) for item in argv)
        cwd = ctx.cwd or location.build_dir or location.project_root
        cwd_label = self._display(cwd, {}, location)
        world = BuildWorld.HOST.value
        digest = tpl.command_digest(display, cwd_label=cwd_label, env_keys=environment.keys,
                                    world=world, network=decision.policy)
        notes = list(ctx.notes) + list(decision.notes) + list(environment.notes)
        return BuildJobPlan(
            action=request.action, system=ctx.system.value, project_root=location.project_root,
            build_dir=ctx.build_dir or location.build_dir, cwd=str(cwd),
            argv=tuple(decision.prefix) + tuple(argv), display_argv=display, cwd_label=cwd_label,
            command_digest=digest, environment=environment.pairs, env_keys=environment.keys,
            timeout_seconds=timeout, max_descendants=max_descendants,
            memory_limit_bytes=self._memory_limit, log_dir=ctx.log_dir, log_file=ctx.log_file,
            binlog=ctx.binlog, world=world, network=decision.policy,
            isolation_truth=ISOLATION_UNVERIFIED, model_digest=model.digest if model else "",
            template_id=template_id, checked_executables=checked + tuple(decision.checked_executables),
            notes=tuple(dict.fromkeys(note for note in notes if note))[:32],
            project_label=location.project_label, target=ctx.target or ctx.validated.target,
            config=ctx.config or ctx.validated.config, platform=ctx.platform or ctx.validated.platform,
            file_label=ctx.file_label, run_token=token, pre_writes=tuple(ctx.pre_writes),
            extra_logs=tuple(ctx.extra_logs), trace_family=ctx.trace_family,
            trace_forced=tuple(ctx.trace_forced), lease_id=lease or "",
        )

    @staticmethod
    def _toolset_hint(model: BuildModel | None) -> str:
        if model is None:
            return ""
        for toolchain in model.toolchains:
            if toolchain.msvc_toolset:
                return toolchain.msvc_toolset
        return ""

    def _display(self, item: str, placeholders: Mapping[str, str], location: BuildTreeLocation) -> str:
        text = str(item)
        for value, name in placeholders.items():
            text = text.replace(value, name)
        if location.build_dir and location.build_dir in text:
            label = location.build_label or BUILD_LABEL
            text = text.replace(location.build_dir, label if label != "." else BUILD_LABEL)
        if location.project_root and location.project_root in text:
            text = text.replace(location.project_root, location.project_label)
        return self._redact(text)


class _AnyContext:
    """Build-dir checks for host-derived dirs (preset binaryDir, default build/)."""

    workspace_roots: tuple = ()


class _PlanContext:
    def __init__(self, *, location, model, request, validated, log_dir, log_file, notes):
        self.location = location
        self.model = model
        self.request = request
        self.validated = validated
        self.log_dir = log_dir
        self.log_file = log_file
        self.notes = notes
        self.template = None
        self.template_id = ""
        self.values: dict[str, str] = {}
        self.lists: dict[str, tuple[str, ...]] = {}
        self.system = BuildSystem.CMAKE
        self.cwd = ""
        self.build_dir = ""
        self.config = ""
        self.platform = ""
        self.target = ""
        self.file_label = ""
        self.generator = ""
        self.binlog = ""
        self.extra_logs: tuple[str, ...] = ()
        self.pre_writes: tuple[tuple[str, bytes], ...] = ()
        self.placeholders: dict[str, str] = {}
        self.vcvars_family = ""
        self.raw_argv: tuple[str, ...] = ()
        self.checked_extra: tuple[str, ...] = ()
        self.trace_family = ""
        self.trace_forced: tuple[str, ...] = ()
        self.template_note = ""
        self.executable_override = ""
        self.profile_daemon = False


__all__ = ["BINLOG_NAME", "MSBUILD_LOG_NAME", "ProjectBuildPlanner"]
