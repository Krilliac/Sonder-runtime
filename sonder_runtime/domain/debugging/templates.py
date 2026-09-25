"""Host-owned argv templates for Tier-1 debuggers and profilers.

Every template is fixed here; callers choose only the engine, the tool path
(from the host inventory), bounded numbers and, for the attended console
only, the network flag. Per-run values are placeholders:

``{nonce}``  16 hex digits bound by the launcher (unforgeable section markers)
``{rundir}`` the private run directory
``{input}``  the staged capture
``{exe}``    the staged/contained executable
``{sympath}``/``{imagepath}`` the sanitized cdb symbol and image paths
``{solibpath}`` the contained gdb solib search path

Plans digest the placeholder argv (``plan_digest``), so the evaluator's plan
and the executor's plan agree across nonces and run dirs. ``materialize``
substitutes the placeholders exactly once, refusing unknown bindings and
values carrying NUL or newlines.

Security properties encoded here (see the crash-profile spec section 4):

- cdb: ``-sflags 0x022802B7`` (IGNORE_CVREC, IGNORE_IMAGEDIR, NO_PROMPTS,
  DISABLE_SYMSRV_AUTODETECT, FAIL_CRITICAL_ERRORS, DEFERRED_LOADS,
  LOAD_LINES, UNDNAME, CASE_INSENSITIVE, OMAP_FIND_NEAREST; never
  LOAD_ANYTHING 0x40), ``-netsyms no`` unless network, ``kn`` not ``kP``;
- gdb: ``-nx -nh``, auto-load off, debuginfod off unless network, ``bt 64``
  (no ``bt full``), nonce markers;
- lldb: settings through ``-O`` (before the target), a nonce prompt, no
  external lookup, no debuginfod, no scripts from symbol files;
- llvm-symbolizer: always ``--no-debuginfod``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ..common.errors import InvalidInput


PLACEHOLDERS = ("nonce", "rundir", "input", "exe", "sympath", "imagepath", "solibpath")
_PLACEHOLDER_RE = re.compile(r"\{(%s)\}" % "|".join(PLACEHOLDERS))
_NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
CDB_SFLAGS = 0x022802B7
SYMOPT_LOAD_ANYTHING = 0x40
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 900
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
NETNS_ARGS = ("--user", "--map-current-user", "--net", "--")
MAX_SYMBOLIZER_ADDRESSES = 64
_MODULE_STEM_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.wpaProfile$")
_ZONE_RE = re.compile(r"^[A-Za-z0-9_:. -]{1,64}$")


class TemplateError(InvalidInput):
    code = "TEMPLATE_INVALID"


@dataclass(frozen=True, slots=True)
class ArgvTemplate:
    engine: str
    argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    parser: str
    max_output_bytes: int = MAX_OUTPUT_BYTES
    timeout_default: int = DEFAULT_TIMEOUT
    timeout_max: int = MAX_TIMEOUT
    needs_executable: bool = False
    reads_capture_via: str = "argv"

    def placeholders(self) -> tuple[str, ...]:
        found = []
        for item in self.argv + tuple(value for _, value in self.env):
            for match in _PLACEHOLDER_RE.finditer(item):
                if match.group(1) not in found:
                    found.append(match.group(1))
        return tuple(found)


def _tool(tool_path: str) -> str:
    text = str(tool_path or "")
    if not text or "\x00" in text or "\n" in text or _PLACEHOLDER_RE.search(text):
        raise TemplateError("tool path is empty or invalid")
    return text


def _template(engine: str, argv: list[str], parser: str, *, env=(), needs_executable=False,
              timeout_default=DEFAULT_TIMEOUT, max_output=MAX_OUTPUT_BYTES,
              reads="argv") -> ArgvTemplate:
    items = tuple(argv)
    return ArgvTemplate(engine=engine, argv=items, display_argv=items, env=tuple(env), parser=parser,
                        max_output_bytes=max_output, timeout_default=timeout_default,
                        timeout_max=MAX_TIMEOUT, needs_executable=needs_executable,
                        reads_capture_via=reads)


# ------------------------------------------------------------------ crash engines

def cdb_template(tool_path: str, *, network: bool = False) -> ArgvTemplate:
    commands = (
        ".echo SONDER_{nonce}_BEGIN;.ecxr;.echo SONDER_{nonce}_STACK;kn 64;"
        ".echo SONDER_{nonce}_ANALYZE;!analyze -v;.echo SONDER_{nonce}_THREADS;~*kn 16;"
        ".echo SONDER_{nonce}_MODULES;lm t n;.echo SONDER_{nonce}_END;q"
    )
    argv = [_tool(tool_path), "-z", "{input}", "-lines", "-noshell", "-sins",
            "-netsyms", "yes" if network else "no", "-sflags", "0x%08X" % CDB_SFLAGS,
            "-y", "{sympath}", "-i", "{imagepath}", "-c", commands]
    return _template("cdb", argv, "cdb")


def gdb_template(tool_path: str, *, network: bool = False) -> ArgvTemplate:
    argv = [
        _tool(tool_path), "-nx", "-nh", "-batch", "-q",
        "-iex", "set auto-load off",
        "-iex", "set debuginfod enabled %s" % ("on" if network else "off"),
        "-iex", "set libthread-db-search-path $sdir",
        "-iex", "set history save off",
        "-iex", "set solib-search-path {solibpath}",
        "-ex", "set pagination off", "-ex", "set width 0", "-ex", "set print elements 64",
        "-ex", "set max-value-size 65536",
        "-ex", "echo \\nSONDER_{nonce}_SIG\\n", "-ex", "p $_siginfo._sifields._sigfault.si_addr",
        "-ex", "echo \\nSONDER_{nonce}_BT\\n", "-ex", "bt 64",
        "-ex", "echo \\nSONDER_{nonce}_THREADS\\n", "-ex", "thread apply all bt 16",
        "-ex", "echo \\nSONDER_{nonce}_LIBS\\n", "-ex", "info sharedlibrary",
        "-ex", "echo \\nSONDER_{nonce}_END\\n",
        "{exe}", "{input}",
    ]
    return _template("gdb", argv, "gdb", needs_executable=True)


def lldb_template(tool_path: str, *, network: bool = False) -> ArgvTemplate:
    argv = [
        _tool(tool_path), "--batch", "--no-lldbinit",
        "-O", "settings set prompt (SONDER_{nonce}) ",
        "-O", "settings set symbols.enable-external-lookup false",
        "-O", "settings clear plugin.symbol-locator.debuginfod.server-urls",
        "-O", "settings set target.load-script-from-symbol-file false",
        "-O", "settings set target.load-cwd-lldbinit false",
        "--core", "{input}", "{exe}",
        "-o", "thread backtrace all -c 32",
        "-o", "image list",
    ]
    # ``network`` is accepted for a uniform builder signature; lldb never does
    # external lookup here (with consent the planner prefers gdb/cdb).
    del network
    return _template("lldb", argv, "lldb", needs_executable=True)


def eu_stack_template(tool_path: str) -> ArgvTemplate:
    argv = [_tool(tool_path), "--core", "{input}", "-e", "{exe}", "-a", "-b", "-i", "-m", "-s", "-n", "64"]
    return _template("eu_stack", argv, "eu_stack", needs_executable=True)


def _stem(module: str) -> str:
    text = str(module or "")
    if not _MODULE_STEM_RE.match(text) or text in (".", ".."):
        raise TemplateError("module name is not a plain file name")
    return text


def dump_syms_template(tool_path: str, module_stem: str, debug_id: str) -> ArgvTemplate:
    """Pre-step for one GUID-verified module, writing the Breakpad layout
    ``{rundir}/syms/<m>.pdb/<DEBUGID>/<m>.sym``."""
    stem = _stem(module_stem)
    ident = str(debug_id or "")
    if not re.match(r"^[0-9A-F]{33,40}$", ident):
        raise TemplateError("debug id must be 33-40 upper-case hex digits")
    argv = [_tool(tool_path), "{rundir}/sym/%s/%s.pdb" % (stem, stem),
            "-o", "{rundir}/syms/%s.pdb/%s/%s.sym" % (stem, ident, stem)]
    return _template("minidump_stackwalk", argv, "dump_syms", timeout_default=300)


def stackwalk_template(tool_path: str, *, breakpad_cpp: bool = False) -> ArgvTemplate:
    if breakpad_cpp:
        argv = [_tool(tool_path), "-m", "{input}", "{rundir}/syms"]
        return _template("minidump_stackwalk", argv, "stackwalk_machine")
    argv = [_tool(tool_path), "--json", "--symbols-path", "{rundir}/syms", "{input}"]
    return _template("minidump_stackwalk", argv, "stackwalk_json")


def symbolizer_template(tool_path: str, module_file: str, addresses, *, elf: bool = False) -> ArgvTemplate:
    """One invocation per verified module; ``module_file`` is ``<m>.exe``/``<m>.dll``/ELF name."""
    name = _stem(module_file)
    values = [int(value) for value in addresses][:MAX_SYMBOLIZER_ADDRESSES + 1]
    if not values or len(values) > MAX_SYMBOLIZER_ADDRESSES or any(v < 0 or v >= 1 << 64 for v in values):
        raise TemplateError("1..64 non-negative relative addresses are required")
    stem = name.rsplit(".", 1)[0] if "." in name and not elf else name
    argv = [_tool(tool_path), "--obj", "{rundir}/sym/%s/%s" % (stem, name), "--no-debuginfod",
            "--inlines", "--demangle", "--relative-address", "--output-style=JSON"]
    if elf:
        argv.append("--debug-file-directory={rundir}/sym")
    argv += ["0x%x" % value for value in values]
    return _template("llvm_symbolizer", argv, "symbolizer_json", timeout_default=60,
                     max_output=4 * 1024 * 1024)


# ------------------------------------------------------------------ profile engines

def perf_templates(tool_path: str) -> tuple[ArgvTemplate, ArgvTemplate]:
    env = (("PERF_CONFIG", "/dev/null"), ("PERF_BUILDID_DIR", "{rundir}/buildid"))
    folded = [_tool(tool_path), "report", "-i", "{input}", "--stdio", "--no-children",
              "--percent-limit", "0.5", "--max-stack", "32", "-g", "folded,0.5,caller,function,percent",
              "--sort", "dso,sym"]
    flat = [_tool(tool_path), "report", "-i", "{input}", "--stdio", "--children", "-g", "none",
            "--percent-limit", "0.3", "--sort", "dso,sym"]
    return (_template("perf", folded, "perf_folded", env=env),
            _template("perf", flat, "perf_flat", env=env))


def heaptrack_print_template(tool_path: str) -> ArgvTemplate:
    argv = [_tool(tool_path), "{input}", "--print-peaks", "1", "--print-allocators", "1",
            "--print-leaks", "1", "--print-temporary", "1", "--peak-limit", "20"]
    return _template("heaptrack_print", argv, "heaptrack_text")


def tracy_csvexport_templates(tool_path: str, *, frame_zone: str = "") -> tuple[ArgvTemplate, ...]:
    steps = [_template("tracy_csvexport", [_tool(tool_path), "{input}"], "tracy_csv")]
    if frame_zone:
        if not _ZONE_RE.match(frame_zone):
            raise TemplateError("frame zone name is not allowed")
        steps.append(_template("tracy_csvexport", [_tool(tool_path), "-u", "{input}"], "tracy_csv_unwrap"))
    return tuple(steps)


def xperf_template(tool_path: str) -> ArgvTemplate:
    argv = [_tool(tool_path), "-i", "{input}", "-o", "{rundir}\\out\\cpu.txt", "-symbols", "-a",
            "profile", "-detail"]
    env = (("_NT_SYMCACHE_PATH", "{rundir}\\symcache"),)
    return _template("xperf", argv, "xperf_text", env=env, reads="file:{rundir}\\out\\cpu.txt")


def wpaexporter_template(tool_path: str, profile_name: str, profile_path: str) -> ArgvTemplate:
    if not _PROFILE_RE.match(str(profile_name or "")):
        raise TemplateError("wpaProfile name is not allowed")
    if not profile_path or "\x00" in profile_path or _PLACEHOLDER_RE.search(profile_path):
        raise TemplateError("wpaProfile path is invalid")
    argv = [_tool(tool_path), "-i", "{input}", "-profile", profile_path, "-outputfolder", "{rundir}\\out"]
    env = (("_NT_SYMCACHE_PATH", "{rundir}\\symcache"),)
    return _template("wpaexporter", argv, "wpa_csv", env=env, reads="dir:{rundir}\\out")


# ------------------------------------------------------------------ environment

def posix_environment(*, network: bool, debuginfod_url: str = "") -> tuple[tuple[str, str], ...]:
    """The complete POSIX environment (inherit_environment=False)."""
    return (
        ("PATH", "/usr/bin:/bin"),
        ("HOME", "{rundir}/home"),
        ("LANG", "C.UTF-8"),
        ("TMPDIR", "{rundir}/tmp"),
        ("DEBUGINFOD_URLS", debuginfod_url if network else ""),
        ("PERF_CONFIG", "/dev/null"),
    )


def windows_environment(system_root: str, tool_dir: str, *, symbol_path: str = "",
                        with_symcache: bool = False) -> tuple[tuple[str, str], ...]:
    """The complete Windows environment; ``_NT_*`` only for xperf/wpaexporter."""
    for value in (system_root, tool_dir):
        if not value or "\x00" in value or ";" in value:
            raise TemplateError("environment path is invalid")
    env = [
        ("SystemRoot", system_root), ("windir", system_root),
        ("TEMP", "{rundir}\\tmp"), ("TMP", "{rundir}\\tmp"),
        ("USERPROFILE", "{rundir}\\home"), ("LOCALAPPDATA", "{rundir}\\home"), ("APPDATA", "{rundir}\\home"),
        ("PATH", "%s;%s\\System32" % (tool_dir, system_root)),
        ("NoDefaultCurrentDirectoryInExePath", "1"),
    ]
    if with_symcache:
        env.append(("_NT_SYMBOL_PATH", symbol_path))
        env.append(("_NT_SYMCACHE_PATH", "{rundir}\\symcache"))
    return tuple(env)


# ------------------------------------------------------------------ binding

def with_netns(template: ArgvTemplate, unshare_path: str) -> ArgvTemplate:
    """Prefix the argv with ``unshare --user --map-current-user --net --``."""
    prefix = (_tool(unshare_path), *NETNS_ARGS)
    return ArgvTemplate(template.engine, prefix + template.argv, prefix + template.display_argv,
                        template.env, template.parser, template.max_output_bytes,
                        template.timeout_default, template.timeout_max, template.needs_executable,
                        template.reads_capture_via)


def _check_binding(name: str, value: str) -> str:
    text = str(value)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise TemplateError("binding %s contains NUL or a newline" % name)
    if _PLACEHOLDER_RE.search(text):
        raise TemplateError("binding %s contains a placeholder" % name)
    if name == "nonce" and not _NONCE_RE.match(text):
        raise TemplateError("nonce must be 16 lower-case hex digits")
    return text


def materialize(template: ArgvTemplate, bindings: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """``(argv, env)`` with every placeholder substituted exactly once.

    Raises ``TemplateError`` for unknown binding names, a placeholder with no
    binding, or values containing NUL/newlines/placeholders.
    """
    unknown = set(bindings) - set(PLACEHOLDERS)
    if unknown:
        raise TemplateError("unknown bindings: %s" % ", ".join(sorted(unknown)))
    values = {name: _check_binding(name, value) for name, value in bindings.items()}

    def substitute(item: str) -> str:
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in values:
                raise TemplateError("no binding for {%s}" % name)
            return values[name]
        return _PLACEHOLDER_RE.sub(replace, item)

    argv = tuple(substitute(item) for item in template.argv)
    env = tuple((key, substitute(value)) for key, value in template.env)
    return argv, env


__all__ = [
    "ArgvTemplate", "CDB_SFLAGS", "DEFAULT_TIMEOUT", "MAX_OUTPUT_BYTES", "MAX_TIMEOUT", "NETNS_ARGS",
    "PLACEHOLDERS", "SYMOPT_LOAD_ANYTHING", "TemplateError", "cdb_template", "dump_syms_template",
    "eu_stack_template", "gdb_template", "heaptrack_print_template", "lldb_template", "materialize",
    "perf_templates", "posix_environment", "stackwalk_template", "symbolizer_template",
    "tracy_csvexport_templates", "windows_environment", "with_netns", "wpaexporter_template",
    "xperf_template",
]
