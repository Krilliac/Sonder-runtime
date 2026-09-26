"""Host-owned, closed argv templates for build jobs.

Every argv a build job launches is ``<executable> <template tokens>``: the
executable is resolved by the host inventory, every token is a literal here
or a placeholder whose value the host computed or validated against the
parsed build model. A model never contributes argv; it only names members of
the model (target, config, platform, preset, file), and those names are
refused unless they are syntactically inert *and* present in the model.

``{net}`` expands to ``network_hardening_args`` and ``{defines}`` to operator
``-D`` entries from ``SONDER_BUILD_PROFILES``; both are host-fixed lists.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .model import (
    CONFIG_RE,
    PLATFORM_RE,
    PRESET_RE,
    BuildAction,
    BuildDomainError,
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    NetworkPolicy,
    PchMode,
    PresetInfo,
    BuildWorld,
    is_absolute,
    loads_bounded_json,
    norm_path,
    rel_under,
    safe_rel,
)
from .tool_targets import BUILD_ALIASES, UTILITY_TARGET_NAMES, TargetSafety


PLACEHOLDERS = frozenset({
    "source_dir", "build_dir", "generator", "config", "target", "jobs", "preset",
    "build_preset", "file_target", "solution", "project_file", "platform", "log_file", "binlog",
    "make_program",
})
LIST_PLACEHOLDERS = frozenset({"net", "defines"})
GENERATOR_VALUES = (
    "Ninja", "Ninja Multi-Config", "Unix Makefiles", "NMake Makefiles",
    "Visual Studio 17 2022", "Visual Studio 16 2019",
)
MSBUILD_REFUSED_TARGETS = frozenset({
    "Rebuild", "Clean", "Publish", "Deploy", "Restore", "Pack", "Run", "Test",
    "VSTest", "Install",
})
_MSBUILD_REFUSED_FOLDED = frozenset(item.casefold() for item in MSBUILD_REFUSED_TARGETS)
MIN_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_TIMEOUT_SECONDS = 7200
OPERATOR_TIMEOUT_CEILING = 86400
MAX_JOBS = 256
MAX_DESCENDANTS = 512
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]+)\}")
_CMAKE_TARGET_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
_MSBUILD_TARGET_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}(?:\\[A-Za-z0-9_.+-]{1,128}){0,8}$")
_MODEL_FORBIDDEN = frozenset("=;,%\"'`$&|<>^\x00")
DEFINE_RE = re.compile(r"^-D[A-Za-z_][A-Za-z0-9_]*(?::[A-Z]+)?=[^\x00\n]{0,512}$")
_BANNED_CMAKE_FLAGS = ("-C", "-P", "-E", "-U", "--graphviz")
_BANNED_CMAKE_PREFIXES = ("--trace", "-L", "--debugger", "--graphviz")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9+._-]{1,64}$")


class TemplateRejected(BuildDomainError):
    """A template, placeholder value or request refused before any launch."""

    def __init__(self, message: str, *, code: str = "INVALID_INPUT", rule: str = "") -> None:
        super().__init__(code, message)
        self.rule = rule or code


@dataclass(frozen=True, slots=True)
class Segment:
    tokens: tuple[str, ...]
    optional: bool = False


@dataclass(frozen=True, slots=True)
class BuildTemplate:
    template_id: str
    system: BuildSystem
    action: BuildAction
    tool: str
    segments: tuple[Segment, ...]
    cwd: str = "build_dir"
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_descendants: int = MAX_DESCENDANTS
    family: str = ""

    def placeholders(self) -> frozenset[str]:
        names: set[str] = set()
        for segment in self.segments:
            for token in segment.tokens:
                names.update(_PLACEHOLDER_RE.findall(token))
        return frozenset(names)


def _seg(*tokens: str, optional: bool = False) -> Segment:
    return Segment(tokens=tuple(tokens), optional=optional)


_CLP = "-clp:NoSummary;ForceNoAlign;DisableConsoleColor"
TEMPLATES: Mapping[str, BuildTemplate] = {
    "cmake.configure": BuildTemplate(
        "cmake.configure", BuildSystem.CMAKE, BuildAction.CONFIGURE, "cmake",
        (_seg("-S", "{source_dir}", "-B", "{build_dir}", "-G", "{generator}",
              "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"),
         _seg("-DCMAKE_BUILD_TYPE={config}", optional=True),
         _seg("-DCMAKE_MAKE_PROGRAM={make_program}", optional=True),
         _seg("{defines}"), _seg("{net}")),
        cwd="source_dir", default_timeout_seconds=600),
    "cmake.configure.preset": BuildTemplate(
        "cmake.configure.preset", BuildSystem.CMAKE, BuildAction.CONFIGURE, "cmake",
        (_seg("--preset", "{preset}"),
         _seg("-DCMAKE_MAKE_PROGRAM={make_program}", optional=True), _seg("{net}")),
        cwd="source_dir", default_timeout_seconds=600),
    "cmake.build": BuildTemplate(
        "cmake.build", BuildSystem.CMAKE, BuildAction.BUILD, "cmake",
        (_seg("--build", "{build_dir}"), _seg("--config", "{config}", optional=True),
         _seg("--target", "{target}", optional=True), _seg("--parallel", "{jobs}"))),
    "cmake.build.preset": BuildTemplate(
        "cmake.build.preset", BuildSystem.CMAKE, BuildAction.BUILD, "cmake",
        (_seg("--build", "--preset", "{build_preset}"),
         _seg("--target", "{target}", optional=True), _seg("--parallel", "{jobs}")),
        cwd="source_dir"),
    "ninja.compile_one": BuildTemplate(
        "ninja.compile_one", BuildSystem.NINJA, BuildAction.COMPILE_ONE, "ninja",
        (_seg("-C", "{build_dir}"), _seg("-f", "build-{config}.ninja", optional=True),
         _seg("{file_target}")),
        default_timeout_seconds=600),
    "make.build": BuildTemplate(
        "make.build", BuildSystem.MAKE, BuildAction.BUILD, "make",
        (_seg("-C", "{build_dir}", "-j{jobs}"), _seg("{target}", optional=True))),
    "msbuild.build": BuildTemplate(
        "msbuild.build", BuildSystem.MSBUILD, BuildAction.BUILD, "msbuild",
        (_seg("{solution}", optional=True), _seg("{project_file}", optional=True),
         _seg("-t:{target}", "-p:Configuration={config}", "-p:Platform={platform}",
              "-m:{jobs}", "-nologo", "-nr:false", "-v:m", _CLP,
              "-flp:LogFile={log_file};Verbosity=normal;Encoding=UTF-8", "-bl:{binlog}"),
         _seg("{net}"))),
    "msbuild.compile_one": BuildTemplate(
        "msbuild.compile_one", BuildSystem.MSBUILD, BuildAction.COMPILE_ONE, "msbuild",
        (_seg("{project_file}", "-t:ClCompile", "-p:SelectedFiles={file_target}",
              "-p:Configuration={config}", "-p:Platform={platform}", "-nologo", "-nr:false",
              "-v:m", _CLP),),
        default_timeout_seconds=600),
}
# INCLUDE_TRACE argv come from compile_db.sanitize_for_trace; these ids name them.
TRACE_TEMPLATE_IDS = frozenset({"trace.gnu", "trace.msvc"})
PROFILE_TEMPLATE_PREFIX = "profile."


def network_hardening_args(system: BuildSystem | str, allow_network: bool = False) -> tuple[str, ...]:
    """Host-fixed arguments that disable package/fetch downloads (none when allowed)."""
    if allow_network:
        return ()
    value = system.value if isinstance(system, BuildSystem) else str(system)
    if value == BuildSystem.CMAKE.value:
        return ("-DFETCHCONTENT_FULLY_DISCONNECTED=ON", "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
                "-DVCPKG_MANIFEST_INSTALL=OFF")
    if value == BuildSystem.MSBUILD.value:
        return ("-p:VcpkgManifestInstall=false", "-p:RestorePackages=false")
    return ()


# --- value validation ---------------------------------------------------------------

def _model_value(value: str, what: str, code: str, *, allow_space: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise TemplateRejected("%s is required" % what, code=code)
    if len(value) > 256:
        raise TemplateRejected("%s is too long" % what, code=code)
    if value[0] in "-/":
        raise TemplateRejected("%s may not start with '-' or '/'" % what, code=code,
                               rule="leading_dash")
    if any(ch in _MODEL_FORBIDDEN for ch in value):
        raise TemplateRejected("%s contains a refused character" % what, code=code,
                               rule="forbidden_character")
    for ch in value:
        if ch.isspace() and not (allow_space and ch == " "):
            raise TemplateRejected("%s contains whitespace" % what, code=code,
                                   rule="whitespace")
        if ord(ch) < 32 or ord(ch) == 127:
            raise TemplateRejected("%s contains a control character" % what, code=code)
    if value != value.strip():
        raise TemplateRejected("%s has surrounding spaces" % what, code=code)
    return value


def validate_target_name(value: str, *, system: BuildSystem) -> str:
    """Syntax of a model-named target for one build system."""
    if system is BuildSystem.MSBUILD:
        text = value
        if text.endswith(":Build"):
            text = text[: -len(":Build")]
        if ":" in text:
            raise TemplateRejected("only the :Build MSBuild target suffix is allowed",
                                   code="UNKNOWN_TARGET", rule="msbuild_suffix")
        # MSBuild target names are case-insensitive: -t:rebuild runs Rebuild.
        if text.casefold() in _MSBUILD_REFUSED_FOLDED:
            raise TemplateRejected("MSBuild target %s is refused" % text,
                                   code="UNKNOWN_TARGET", rule="msbuild_refused")
        _model_value(text.replace("\\", "_"), "target", "UNKNOWN_TARGET")
        if not _MSBUILD_TARGET_RE.fullmatch(text):
            raise TemplateRejected("invalid MSBuild target", code="UNKNOWN_TARGET")
        return value
    _model_value(value, "target", "UNKNOWN_TARGET")
    if ":" in value or not _CMAKE_TARGET_RE.fullmatch(value):
        raise TemplateRejected("invalid target name", code="UNKNOWN_TARGET", rule="target_syntax")
    return value


def _host_path(value: str, what: str, *, suffixes: tuple[str, ...] = ()) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise TemplateRejected("%s must be a host path" % what)
    if any(ch in value for ch in "\x00\r\n;\""):
        raise TemplateRejected("%s contains a refused character" % what)
    if not is_absolute(value):
        raise TemplateRejected("%s must be absolute" % what)
    if value.replace("\\", "/").startswith("//"):
        # UNC and device paths (\\server\share, \\?\, \\.\) leave the
        # local disk and can hand the account's credentials to a remote host.
        raise TemplateRejected("%s may not be a UNC or device path" % what)
    if suffixes and not value.lower().endswith(suffixes):
        raise TemplateRejected("%s has an unexpected suffix" % what)
    return value


def _file_target(value: str, *, template_id: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise TemplateRejected("file target is required", code="UNKNOWN_FILE")
    if value[0] in "-@" or any(ch in value for ch in "\x00\r\n;,%\"'`$&|<>"):
        raise TemplateRejected("file target contains a refused character", code="UNKNOWN_FILE")
    if template_id == "ninja.compile_one":
        if not value.endswith("^") or "^" in value[:-1]:
            raise TemplateRejected("ninja file target must end with ^", code="UNKNOWN_FILE")
    elif "^" in value:
        raise TemplateRejected("file target contains a refused character", code="UNKNOWN_FILE")
    return value


def validate_placeholder(name: str, value: str, *, template: BuildTemplate) -> str:
    if name not in PLACEHOLDERS:
        raise TemplateRejected("unknown placeholder %r" % name[:40], rule="unknown_placeholder")
    if name in ("source_dir", "build_dir", "make_program"):
        return _host_path(value, name)
    if name == "log_file":
        return _host_path(value, name, suffixes=(".log",))
    if name == "binlog":
        return _host_path(value, name, suffixes=(".binlog",))
    if name == "solution":
        return _host_path(value, name, suffixes=(".sln",))
    if name == "project_file":
        return _host_path(value, name, suffixes=(".vcxproj",))
    if name == "generator":
        if value not in GENERATOR_VALUES:
            raise TemplateRejected("unsupported generator")
        return value
    if name == "jobs":
        if not isinstance(value, str) or not value.isascii() or not value.isdigit() \
                or not 1 <= int(value) <= MAX_JOBS:
            raise TemplateRejected("jobs must be 1..%d" % MAX_JOBS)
        return str(int(value))
    if name == "config":
        _model_value(value, "config", "UNKNOWN_CONFIG")
        if not CONFIG_RE.fullmatch(value):
            raise TemplateRejected("invalid config", code="UNKNOWN_CONFIG")
        return value
    if name == "platform":
        _model_value(value, "platform", "UNKNOWN_PLATFORM", allow_space=True)
        if not PLATFORM_RE.fullmatch(value):
            raise TemplateRejected("invalid platform", code="UNKNOWN_PLATFORM")
        return value
    if name in ("preset", "build_preset"):
        _model_value(value, name, "UNKNOWN_PRESET")
        if not PRESET_RE.fullmatch(value):
            raise TemplateRejected("invalid preset", code="UNKNOWN_PRESET")
        return value
    if name == "target":
        return validate_target_name(value, system=template.system)
    if name == "file_target":
        return _file_target(value, template_id=template.template_id)
    raise TemplateRejected("unhandled placeholder")  # pragma: no cover - PLACEHOLDERS is closed


def validate_define(entry: str) -> str:
    if not isinstance(entry, str) or not DEFINE_RE.fullmatch(entry):
        raise TemplateRejected("operator define must match -DNAME[:TYPE]=value",
                               rule="operator_define")
    return entry


def build_argv(template: BuildTemplate | str, *, executable: str,
               values: Mapping[str, str],
               lists: Mapping[str, Iterable[str]] | None = None) -> tuple[str, ...]:
    """Render a closed template. Optional segments render only when every
    placeholder in them has a value; required ones raise when any is missing."""
    if isinstance(template, str):
        if template not in TEMPLATES:
            raise TemplateRejected("unknown template %r" % template[:64], rule="unknown_template")
        template = TEMPLATES[template]
    if not isinstance(template, BuildTemplate):
        raise TemplateRejected("not a build template")
    _host_path(executable, "executable")
    unknown = set(values) - PLACEHOLDERS
    if unknown:
        raise TemplateRejected("unknown placeholder %r" % sorted(unknown)[0][:40],
                               rule="unknown_placeholder")
    lists = dict(lists or {})
    unknown_lists = set(lists) - LIST_PLACEHOLDERS
    if unknown_lists:
        raise TemplateRejected("unknown list placeholder", rule="unknown_placeholder")
    if values.get("solution") and values.get("project_file") and template.template_id == "msbuild.build":
        raise TemplateRejected("msbuild takes a solution or a project file, not both")
    checked = {name: validate_placeholder(name, value, template=template)
               for name, value in values.items() if value not in (None, "")}
    argv: list[str] = [executable]
    for segment in template.segments:
        names = [name for token in segment.tokens for name in _PLACEHOLDER_RE.findall(token)]
        for name in names:
            if name not in PLACEHOLDERS and name not in LIST_PLACEHOLDERS:
                raise TemplateRejected("template names unknown placeholder %r" % name,
                                       rule="unknown_placeholder")
        if len(segment.tokens) == 1 and names and names[0] in LIST_PLACEHOLDERS:
            items = tuple(lists.get(names[0], ()))
            if names[0] == "net":
                allowed = network_hardening_args(template.system, False)
                if items and items != allowed:
                    raise TemplateRejected("network arguments are host-fixed", rule="net")
            else:
                items = tuple(validate_define(item) for item in items)
            argv.extend(items)
            continue
        missing = [name for name in names if name not in checked]
        if missing:
            if segment.optional:
                continue
            raise TemplateRejected("missing value for {%s}" % missing[0], rule="missing_value")
        for token in segment.tokens:
            argv.append(_PLACEHOLDER_RE.sub(lambda m: checked[m.group(1)], token))
    if template.template_id == "msbuild.build" and not (checked.get("solution")
                                                         or checked.get("project_file")):
        raise TemplateRejected("msbuild needs a solution or a project file", rule="missing_value")
    return tuple(argv)


def command_digest(argv: Iterable[str], *, cwd_label: str, env_keys: Iterable[str],
                   world: BuildWorld | str, network: NetworkPolicy | str) -> str:
    """Stable digest binding approval to argv, cwd, env key names, world and network."""
    payload = {
        "argv": [str(item) for item in argv],
        "cwd": str(cwd_label),
        "env_keys": sorted({str(item) for item in env_keys}),
        "world": world.value if isinstance(world, BuildWorld) else str(world),
        "network": network.value if isinstance(network, NetworkPolicy) else str(network),
    }
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def clamp_timeout(requested: object, template: BuildTemplate | None = None,
                  operator_max: object = None) -> int:
    """Seconds for a job: template default, capped at 7200 or the operator cap (<= 86400)."""
    try:
        cap = int(operator_max) if operator_max not in (None, "") else MAX_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        cap = MAX_TIMEOUT_SECONDS
    cap = max(MIN_TIMEOUT_SECONDS, min(cap, OPERATOR_TIMEOUT_CEILING))
    default = template.default_timeout_seconds if template is not None else DEFAULT_TIMEOUT_SECONDS
    if requested in (None, ""):
        return max(MIN_TIMEOUT_SECONDS, min(default, cap))
    try:
        value = int(requested)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(MIN_TIMEOUT_SECONDS, min(default, cap))
    return max(MIN_TIMEOUT_SECONDS, min(value, cap))


def clamp_jobs(requested: object, cpu_count: int) -> int:
    ceiling = max(1, min(int(cpu_count or 1), MAX_JOBS))
    try:
        value = int(requested) if requested not in (None, "") else ceiling  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = ceiling
    return max(1, min(value, ceiling))


# --- request validation -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValidatedBuildRequest:
    action: BuildAction
    target: str = ""
    target_info: BuildTarget | None = None
    config: str = ""
    platform: str = ""
    preset: PresetInfo | None = None
    build_preset: PresetInfo | None = None
    unit: CompileUnit | None = None
    generator: str = ""
    needs_target_build: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def _field(request: object, name: str) -> str:
    value = getattr(request, name, "") if not isinstance(request, Mapping) else request.get(name, "")
    if value is None:
        return ""
    if hasattr(value, "value") and not isinstance(value, str):
        value = value.value
    return value if isinstance(value, str) else str(value)


def validate_request_against_model(
    request: object,
    model: BuildModel,
    safety: TargetSafety,
    *,
    operator_utility_allow: frozenset[str] = frozenset(),
) -> ValidatedBuildRequest:
    """Check every model-named value of a build request against the parsed model.

    ``request`` is any object (or mapping) with ``action``, ``target``,
    ``config``, ``platform``, ``preset``, ``build_preset``, ``file`` and
    ``generator`` fields.
    """
    try:
        action = BuildAction(_field(request, "action") or BuildAction.BUILD.value)
    except ValueError as exc:
        raise TemplateRejected("unknown build action", code="ACTION_UNSUPPORTED") from exc
    notes: list[str] = []
    target = _field(request, "target")
    target_info = None
    if target:
        validate_target_name(target, system=model.system)
        bare = target[: -len(":Build")] if model.system is BuildSystem.MSBUILD \
            and target.endswith(":Build") else target
        allowed = operator_utility_allow
        if bare in BUILD_ALIASES:
            pass
        else:
            target_info = model.target(bare)
            if target_info is None:
                raise TemplateRejected("target is not in the build model", code="UNKNOWN_TARGET")
            refused = (bare in safety.utility or bare in UTILITY_TARGET_NAMES
                       or target_info.utility)
            if refused and bare not in allowed:
                raise TemplateRejected(
                    "target %s runs arbitrary commands; an operator must allowlist it in "
                    "SONDER_BUILD_UTILITY_TARGETS" % bare, code="UTILITY_TARGET_REFUSED")
            if refused:
                notes.append("utility target %s allowed by operator allowlist" % bare)
    config = _field(request, "config")
    if config:
        validate_placeholder("config", config, template=TEMPLATES["cmake.build"])
        if config not in model.configs:
            raise TemplateRejected("config is not in the build model", code="UNKNOWN_CONFIG")
    platform = _field(request, "platform")
    if platform:
        validate_placeholder("platform", platform, template=TEMPLATES["msbuild.build"])
        if platform not in model.platforms:
            raise TemplateRejected("platform is not in the build model", code="UNKNOWN_PLATFORM")
    preset_info = build_preset_info = None
    preset = _field(request, "preset")
    if preset:
        validate_placeholder("preset", preset, template=TEMPLATES["cmake.configure.preset"])
        preset_info = next((item for item in model.presets
                            if item.kind == "configure" and item.name == preset), None)
        if preset_info is None or preset_info.hidden:
            raise TemplateRejected("preset is not known", code="UNKNOWN_PRESET")
        if not preset_info.binary_dir_resolvable or not preset_info.binary_dir:
            raise TemplateRejected("preset binaryDir depends on the environment",
                                   code="UNKNOWN_PRESET")
        if model.source_root and rel_under(model.source_root, preset_info.binary_dir) is not None:
            raise TemplateRejected("preset binaryDir equals or contains the source dir",
                                   code="UNKNOWN_PRESET")
    build_preset = _field(request, "build_preset")
    if build_preset:
        validate_placeholder("build_preset", build_preset,
                             template=TEMPLATES["cmake.build.preset"])
        build_preset_info = next((item for item in model.presets
                                  if item.kind == "build" and item.name == build_preset), None)
        if build_preset_info is None or build_preset_info.hidden \
                or not build_preset_info.binary_dir_resolvable:
            raise TemplateRejected("build preset is not known", code="UNKNOWN_PRESET")
    generator = _field(request, "generator")
    if generator and generator not in GENERATOR_VALUES:
        raise TemplateRejected("unsupported generator", code="ACTION_UNSUPPORTED")
    if action is BuildAction.CONFIGURE and model.system is BuildSystem.MSBUILD:
        raise TemplateRejected("MSBuild trees have no configure step", code="ACTION_UNSUPPORTED")
    unit = None
    needs_target = False
    if action in (BuildAction.COMPILE_ONE, BuildAction.INCLUDE_TRACE):
        raw = _field(request, "file")
        rel = safe_rel(raw.replace("\\", "/")) if raw and "\x00" not in raw else None
        if rel is None:
            raise TemplateRejected("file must be a source-relative path", code="UNKNOWN_FILE")
        candidates = model.units_for(rel)
        if not candidates and re.match(r"^[A-Za-z]:/", norm_path(model.source_root)):
            candidates = tuple(item for item in model.units
                               if item.file_rel.casefold() == rel.casefold())
        if not candidates:
            raise TemplateRejected("file is not a compile unit of the model", code="UNKNOWN_FILE")
        unit = next((item for item in candidates if target and item.target == target),
                    candidates[0])
        if action is BuildAction.INCLUDE_TRACE and not model.compile_db_available:
            raise TemplateRejected("include_trace needs a compile database (Ninja or Makefile "
                                   "generators)", code="RUNNER_UNAVAILABLE")
        if action is BuildAction.COMPILE_ONE:
            owner = model.target(unit.target)
            if owner is not None and owner.unity and not unit.unity_blob_rel:
                needs_target = True
                notes.append("unity build without a mapped blob: verifying with a target build")
            if unit.pch is PchMode.CREATE:
                needs_target = True
                notes.append("precompiled header creation unit: verifying with a target build")
    return ValidatedBuildRequest(
        action=action, target=target, target_info=target_info, config=config, platform=platform,
        preset=preset_info, build_preset=build_preset_info, unit=unit, generator=generator,
        needs_target_build=needs_target, notes=tuple(notes),
    )


# --- operator profiles ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BuildProfile:
    name: str
    executable: str
    actions: tuple[tuple[str, tuple[str, ...]], ...]
    defines: tuple[str, ...] = ()
    network_daemon: bool = False

    def template(self, action: BuildAction) -> BuildTemplate | None:
        for name, tokens in self.actions:
            if name == action.value:
                return BuildTemplate(
                    PROFILE_TEMPLATE_PREFIX + self.name + "." + name, BuildSystem.PROFILE,
                    action, self.executable, (Segment(tokens=tokens),),
                    cwd="build_dir" if action is not BuildAction.CONFIGURE else "source_dir",
                )
        return None


PROFILE_ACTIONS = frozenset({BuildAction.CONFIGURE.value, BuildAction.BUILD.value,
                             BuildAction.COMPILE_ONE.value})


def _profile_token(token: object, *, cmake: bool) -> str:
    if not isinstance(token, str) or not token or len(token) > 512:
        raise TemplateRejected("profile argv entries must be short strings", rule="profile")
    if any(ch in token for ch in "\x00\r\n"):
        raise TemplateRejected("profile argv entry contains a control character", rule="profile")
    stripped = _PLACEHOLDER_RE.sub("", token)
    if "{" in stripped or "}" in stripped:
        raise TemplateRejected("profile argv entry has a malformed placeholder", rule="profile")
    for name in _PLACEHOLDER_RE.findall(token):
        if name not in PLACEHOLDERS:
            raise TemplateRejected("profile names unknown placeholder %r" % name[:40],
                                   rule="unknown_placeholder")
    if token in UTILITY_TARGET_NAMES or token.lower() in ("deploy", "upload_symbols"):
        raise TemplateRejected("profiles may not name utility actions", rule="profile_utility")
    if cmake and (token in _BANNED_CMAKE_FLAGS or token.startswith(_BANNED_CMAKE_PREFIXES)):
        raise TemplateRejected("profile uses a banned cmake flag", rule="profile_banned_flag")
    return token


def parse_build_profiles(data: bytes) -> tuple[BuildProfile, ...]:
    """Operator ``SONDER_BUILD_PROFILES`` document (the adapter checks file ownership)."""
    document = loads_bounded_json(data, max_bytes=256 * 1024, what="build profiles",
                                  code="BUILD_TOOLS_UNAVAILABLE")
    raw = document.get("profiles") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        raise TemplateRejected("build profiles need a 'profiles' list", rule="profile")
    out: list[BuildProfile] = []
    names: set[str] = set()
    for item in raw[:64]:
        if not isinstance(item, dict):
            raise TemplateRejected("profile entries must be objects", rule="profile")
        name = item.get("name")
        if not isinstance(name, str) or not PRESET_RE.fullmatch(name) or name in names:
            raise TemplateRejected("profile names must be unique identifiers", rule="profile")
        names.add(name)
        executable = item.get("executable")
        if not isinstance(executable, str) or not (
                _TOOL_NAME_RE.fullmatch(executable) or (is_absolute(executable)
                                                     and "\x00" not in executable)):
            raise TemplateRejected("profile executable must be a tool name or absolute path",
                                   rule="profile")
        cmake = executable.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("cmake", "cmake.exe")
        actions_raw = item.get("actions")
        if not isinstance(actions_raw, dict) or not actions_raw:
            raise TemplateRejected("profile needs actions", rule="profile")
        actions: list[tuple[str, tuple[str, ...]]] = []
        for action, tokens in actions_raw.items():
            if action not in PROFILE_ACTIONS:
                raise TemplateRejected("profiles may only define configure/build/compile_one",
                                       rule="profile_utility")
            if not isinstance(tokens, list) or len(tokens) > 64:
                raise TemplateRejected("profile argv must be a short list", rule="profile")
            actions.append((action, tuple(_profile_token(token, cmake=cmake) for token in tokens)))
        defines = tuple(validate_define(entry) for entry in (item.get("defines") or ())[:64])
        out.append(BuildProfile(name=name, executable=executable, actions=tuple(actions),
                                defines=defines, network_daemon=bool(item.get("network_daemon"))))
    return tuple(out)


__all__ = [
    "BuildProfile", "BuildTemplate", "DEFAULT_TIMEOUT_SECONDS", "DEFINE_RE", "GENERATOR_VALUES",
    "LIST_PLACEHOLDERS", "MAX_DESCENDANTS", "MAX_JOBS", "MAX_TIMEOUT_SECONDS",
    "MSBUILD_REFUSED_TARGETS", "PLACEHOLDERS", "PROFILE_TEMPLATE_PREFIX", "Segment",
    "TEMPLATES", "TRACE_TEMPLATE_IDS", "TemplateRejected", "ValidatedBuildRequest",
    "build_argv", "clamp_jobs", "clamp_timeout", "command_digest", "network_hardening_args",
    "parse_build_profiles", "validate_define", "validate_placeholder",
    "validate_request_against_model", "validate_target_name",
]
