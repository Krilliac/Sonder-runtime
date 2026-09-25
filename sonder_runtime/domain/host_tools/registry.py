"""Host-owned registry of inventoried developer tools.

Every entry names the executables to look for and the ONLY argv the adapter
may launch to learn a version.  ``version_args=None`` means presence-only:
the executable is never started.  That is used for GUI/IDE launchers, for
tools whose version query may contact the network, self-update, start a
daemon, download a toolchain, or prompt, and for tools whose version comes
from metadata (MSVC ``cl``/``link`` through vswhere).

Model and HTTP callers can only filter this registry; they can never add an
executable, argument, or environment variable to it.
"""
from __future__ import annotations

from .model import BRIEF_EXCLUDED_NAMES, ToolCategory, ToolSpec

C = ToolCategory
V = ("--version",)
ANY = frozenset({"any"})
WIN = frozenset({"Windows"})
MAC = frozenset({"Darwin"})
LINUX = frozenset({"Linux"})
UNIX = frozenset({"Linux", "Darwin"})


def _spec(
    name: str,
    category: ToolCategory,
    version_args: tuple[str, ...] | None = V,
    *,
    executables: tuple[str, ...] | None = None,
    platforms: frozenset[str] = ANY,
    pattern: str | None = None,
    probe_env: tuple[tuple[str, str], ...] = (),
) -> ToolSpec:
    kwargs = {}
    if pattern is not None:
        kwargs["version_pattern"] = pattern
    return ToolSpec(
        name=name,
        category=category,
        executables=(name,) if executables is None else executables,
        version_args=version_args,
        platforms=platforms,
        in_brief=name not in BRIEF_EXCLUDED_NAMES,
        probe_env=probe_env,
        **kwargs,
    )


HOST_TOOL_SPECS: tuple[ToolSpec, ...] = (
    # -- compilers ----------------------------------------------------------
    _spec("gcc", C.COMPILER),
    _spec("g++", C.COMPILER),
    _spec("clang", C.COMPILER),
    _spec("clang++", C.COMPILER),
    _spec("cl", C.COMPILER, None, platforms=WIN),
    # clang-cl ships versioned only on Debian/Ubuntu (clang-cl-18).
    _spec("clang-cl", C.COMPILER, executables=("clang-cl", "clang-cl-18")),
    _spec("rustc", C.COMPILER),
    _spec("javac", C.COMPILER, ("-version",), pattern=r"javac (\d+(?:\.\d+){0,3})"),
    _spec("kotlinc", C.COMPILER, ("-version",), pattern=r"kotlinc-jvm (\d+(?:\.\d+){1,3})"),
    _spec("swiftc", C.COMPILER),
    _spec("zig", C.COMPILER, ("version",)),
    _spec("nvcc", C.COMPILER, pattern=r"release (\d+(?:\.\d+){1,3})"),
    _spec("gfortran", C.COMPILER),
    _spec("tsc", C.COMPILER),
    _spec("lean", C.COMPILER),
    _spec("xcode-clt", C.COMPILER, None, executables=(), platforms=MAC),
    # -- build systems ------------------------------------------------------
    _spec("cmake", C.BUILD_SYSTEM),
    _spec("ninja", C.BUILD_SYSTEM, executables=("ninja", "ninja-build")),
    _spec("make", C.BUILD_SYSTEM, executables=("make", "gmake")),
    _spec("mingw32-make", C.BUILD_SYSTEM, platforms=WIN),
    _spec("nmake", C.BUILD_SYSTEM, None, platforms=WIN),
    _spec("link", C.BUILD_SYSTEM, None, platforms=WIN),
    _spec("lld-link", C.BUILD_SYSTEM),
    _spec("ld.lld", C.BUILD_SYSTEM),
    # FASTBuild (operator build profiles); -version prints and exits.
    _spec("fbuild", C.BUILD_SYSTEM, ("-version",), pattern=r"FASTBuild v(\d+(?:\.\d+){0,3})"),
    _spec("msbuild", C.BUILD_SYSTEM, ("-version", "-nologo")),
    _spec("meson", C.BUILD_SYSTEM),
    _spec("bazel", C.BUILD_SYSTEM),
    # bazelisk downloads a Bazel release on first use; presence only.
    _spec("bazelisk", C.BUILD_SYSTEM, None),
    _spec("scons", C.BUILD_SYSTEM),
    _spec("gradle", C.BUILD_SYSTEM, pattern=r"Gradle (\d+(?:\.\d+){1,3})"),
    _spec("mvn", C.BUILD_SYSTEM, pattern=r"Apache Maven (\d+(?:\.\d+){1,3})"),
    _spec("ant", C.BUILD_SYSTEM, ("-version",), pattern=r"version (\d+(?:\.\d+){1,3})"),
    # sbt may download its launcher and Scala on first run; presence only.
    _spec("sbt", C.BUILD_SYSTEM, None),
    _spec("cargo", C.BUILD_SYSTEM),
    _spec("dotnet", C.BUILD_SYSTEM),
    _spec("xmake", C.BUILD_SYSTEM),
    _spec("premake5", C.BUILD_SYSTEM),
    _spec("pkg-config", C.BUILD_SYSTEM),
    _spec("lake", C.BUILD_SYSTEM),
    _spec("windows-sdk", C.BUILD_SYSTEM, None, executables=(), platforms=WIN),
    _spec("ccache", C.BUILD_SYSTEM),
    _spec("sccache", C.BUILD_SYSTEM),
    # -- test runners -------------------------------------------------------
    _spec("pytest", C.TEST_RUNNER),
    _spec("tox", C.TEST_RUNNER),
    _spec("nox", C.TEST_RUNNER),
    _spec("ctest", C.TEST_RUNNER),
    _spec("jest", C.TEST_RUNNER),
    _spec("vitest", C.TEST_RUNNER),
    _spec("mocha", C.TEST_RUNNER),
    _spec("cargo-nextest", C.TEST_RUNNER, ("nextest", "--version")),
    _spec("phpunit", C.TEST_RUNNER),
    _spec("rspec", C.TEST_RUNNER),
    # -- linters / formatters ----------------------------------------------
    _spec("ruff", C.LINTER_FORMATTER),
    _spec("flake8", C.LINTER_FORMATTER),
    _spec("pylint", C.LINTER_FORMATTER),
    _spec("black", C.LINTER_FORMATTER),
    _spec("isort", C.LINTER_FORMATTER),
    _spec("mypy", C.LINTER_FORMATTER),
    # The pip "pyright" wrapper downloads Node pyright on first use.
    _spec("pyright", C.LINTER_FORMATTER, None),
    _spec("eslint", C.LINTER_FORMATTER),
    _spec("prettier", C.LINTER_FORMATTER),
    _spec("biome", C.LINTER_FORMATTER),
    _spec("clang-format", C.LINTER_FORMATTER),
    _spec("clang-tidy", C.LINTER_FORMATTER),
    _spec("clangd", C.LINTER_FORMATTER, executables=("clangd", "clangd-18", "clangd-17")),
    _spec("cppcheck", C.LINTER_FORMATTER),
    _spec("rustfmt", C.LINTER_FORMATTER),
    # gofmt has no version switch.
    _spec("gofmt", C.LINTER_FORMATTER, None),
    _spec("golangci-lint", C.LINTER_FORMATTER),
    _spec("shellcheck", C.LINTER_FORMATTER),
    _spec("shfmt", C.LINTER_FORMATTER),
    _spec("hadolint", C.LINTER_FORMATTER),
    _spec("yamllint", C.LINTER_FORMATTER),
    # -- debuggers / profilers ---------------------------------------------
    _spec("gdb", C.DEBUGGER_PROFILER),
    _spec("lldb", C.DEBUGGER_PROFILER),
    _spec("valgrind", C.DEBUGGER_PROFILER, platforms=UNIX),
    _spec("perf", C.DEBUGGER_PROFILER, platforms=LINUX),
    _spec("strace", C.DEBUGGER_PROFILER, ("-V",), platforms=LINUX),
    _spec("ltrace", C.DEBUGGER_PROFILER, ("-V",), platforms=LINUX),
    _spec("rr", C.DEBUGGER_PROFILER, platforms=LINUX),
    _spec("cdb", C.DEBUGGER_PROFILER, None, platforms=WIN),
    _spec("windbg", C.DEBUGGER_PROFILER, None, platforms=WIN),
    _spec("py-spy", C.DEBUGGER_PROFILER),
    _spec("heaptrack", C.DEBUGGER_PROFILER, ("-v",), platforms=LINUX),
    _spec("xperf", C.DEBUGGER_PROFILER, None, platforms=WIN),
    _spec("wpaexporter", C.DEBUGGER_PROFILER, None, platforms=WIN),
    # -- package managers ---------------------------------------------------
    _spec("pip", C.PACKAGE_MANAGER),
    _spec("pip3", C.PACKAGE_MANAGER),
    _spec("uv", C.PACKAGE_MANAGER),
    _spec("pipx", C.PACKAGE_MANAGER),
    _spec("poetry", C.PACKAGE_MANAGER),
    _spec("conda", C.PACKAGE_MANAGER),
    _spec("mamba", C.PACKAGE_MANAGER),
    _spec("npm", C.PACKAGE_MANAGER),
    _spec("npx", C.PACKAGE_MANAGER),
    _spec("pnpm", C.PACKAGE_MANAGER),
    _spec("yarn", C.PACKAGE_MANAGER),
    _spec("bun", C.PACKAGE_MANAGER),
    # nuget.exe prints its whole help (and may self-update) for any switch.
    _spec("nuget", C.PACKAGE_MANAGER, None),
    _spec("vcpkg", C.PACKAGE_MANAGER, ("version",), probe_env=(("VCPKG_DISABLE_METRICS", "1"),)),
    _spec("conan", C.PACKAGE_MANAGER),
    _spec("apt", C.PACKAGE_MANAGER, platforms=LINUX),
    _spec("dnf", C.PACKAGE_MANAGER, platforms=LINUX),
    _spec("pacman", C.PACKAGE_MANAGER, platforms=LINUX),
    _spec("zypper", C.PACKAGE_MANAGER, platforms=LINUX),
    _spec("apk", C.PACKAGE_MANAGER, platforms=LINUX),
    _spec("brew", C.PACKAGE_MANAGER, platforms=UNIX),
    _spec("port", C.PACKAGE_MANAGER, ("version",), platforms=MAC),
    _spec("winget", C.PACKAGE_MANAGER, platforms=WIN),
    # scoop's version command walks and git-queries every bucket.
    _spec("scoop", C.PACKAGE_MANAGER, None, platforms=WIN),
    _spec("choco", C.PACKAGE_MANAGER, platforms=WIN),
    _spec("gem", C.PACKAGE_MANAGER),
    _spec("bundle", C.PACKAGE_MANAGER),
    _spec("composer", C.PACKAGE_MANAGER, probe_env=(("COMPOSER_NO_INTERACTION", "1"),)),
    _spec("elan", C.PACKAGE_MANAGER),
    # -- runtimes -----------------------------------------------------------
    _spec("python", C.RUNTIME),
    _spec("python3", C.RUNTIME),
    _spec("py", C.RUNTIME, platforms=WIN),
    _spec("node", C.RUNTIME),
    _spec("deno", C.RUNTIME, probe_env=(("DENO_NO_UPDATE_CHECK", "1"),)),
    _spec("java", C.RUNTIME, ("-version",), pattern=r"version \"(\d+(?:\.\d+){0,3})"),
    _spec("ruby", C.RUNTIME),
    _spec("perl", C.RUNTIME, pattern=r"\(v(\d+(?:\.\d+){1,3})\)"),
    _spec("php", C.RUNTIME),
    _spec("lua", C.RUNTIME, ("-v",)),
    _spec("Rscript", C.RUNTIME),
    _spec("julia", C.RUNTIME),
    # erl has no print-and-exit version switch that avoids starting a shell.
    _spec("erl", C.RUNTIME, None),
    _spec("elixir", C.RUNTIME, pattern=r"Elixir (\d+(?:\.\d+){1,3})"),
    _spec("go", C.RUNTIME, ("version",), pattern=r"go(\d+(?:\.\d+){1,3})"),
    # -- containers / VMs ---------------------------------------------------
    _spec("docker", C.CONTAINER_VM),
    _spec("podman", C.CONTAINER_VM),
    _spec("nerdctl", C.CONTAINER_VM),
    _spec("kubectl", C.CONTAINER_VM, ("version", "--client")),
    _spec("helm", C.CONTAINER_VM, ("version", "--short")),
    _spec("minikube", C.CONTAINER_VM, ("version",),
          probe_env=(("MINIKUBE_WANTUPDATENOTIFICATION", "false"),)),
    _spec("kind", C.CONTAINER_VM, ("version",)),
    _spec("vagrant", C.CONTAINER_VM, probe_env=(("VAGRANT_CHECKPOINT_DISABLE", "1"),)),
    _spec("qemu-system-x86_64", C.CONTAINER_VM),
    _spec("VBoxManage", C.CONTAINER_VM, ("--version",)),
    _spec("wsl", C.CONTAINER_VM, None, platforms=WIN),
    # multipass version queries its daemon.
    _spec("multipass", C.CONTAINER_VM, None),
    _spec("limactl", C.CONTAINER_VM, platforms=UNIX),
    # -- version control ----------------------------------------------------
    _spec("git", C.VCS),
    _spec("gh", C.VCS),
    _spec("glab", C.VCS, probe_env=(("GLAB_CHECK_UPDATE", "false"),)),
    _spec("hg", C.VCS),
    _spec("svn", C.VCS, ("--version", "--quiet")),
    _spec("git-lfs", C.VCS),
    _spec("p4", C.VCS, ("-V",), pattern=r"/(\d{4}\.\d+)/"),
    # -- database clients ---------------------------------------------------
    _spec("sqlite3", C.DB_CLIENT),
    _spec("psql", C.DB_CLIENT),
    _spec("mysql", C.DB_CLIENT),
    _spec("mariadb", C.DB_CLIENT),
    _spec("mongosh", C.DB_CLIENT),
    _spec("redis-cli", C.DB_CLIENT),
    # sqlcmd variants disagree on version switches; presence only.
    _spec("sqlcmd", C.DB_CLIENT, None),
    _spec("duckdb", C.DB_CLIENT, ("-version",)),
    # -- media / documents --------------------------------------------------
    _spec("ffmpeg", C.MEDIA_DOC, ("-version",)),
    _spec("ffprobe", C.MEDIA_DOC, ("-version",)),
    _spec("magick", C.MEDIA_DOC),
    _spec("pandoc", C.MEDIA_DOC),
    _spec("tesseract", C.MEDIA_DOC),
    _spec("gs", C.MEDIA_DOC, executables=("gs", "gswin64c", "gswin32c")),
    _spec("inkscape", C.MEDIA_DOC, None),
    _spec("blender", C.MEDIA_DOC, None),
    _spec("dot", C.MEDIA_DOC, ("-V",)),
    _spec("latexmk", C.MEDIA_DOC),
    _spec("pdflatex", C.MEDIA_DOC, pattern=r"pdfTeX [0-9.]+-[0-9.]+-(\d+(?:\.\d+){1,3})"),
    _spec("qpdf", C.MEDIA_DOC),
    _spec("exiftool", C.MEDIA_DOC, ("-ver",)),
    _spec("sox", C.MEDIA_DOC),
    _spec("doxygen", C.MEDIA_DOC),
    # -- cloud CLIs (most version queries phone home: presence only) -------
    _spec("aws", C.CLOUD_CLI),
    _spec("az", C.CLOUD_CLI, None),
    _spec("gcloud", C.CLOUD_CLI, None),
    _spec("doctl", C.CLOUD_CLI, None),
    _spec("flyctl", C.CLOUD_CLI, None),
    _spec("heroku", C.CLOUD_CLI, None),
    _spec("vercel", C.CLOUD_CLI, None),
    _spec("netlify", C.CLOUD_CLI, None),
    _spec("terraform", C.CLOUD_CLI, ("-version",), probe_env=(("CHECKPOINT_DISABLE", "1"),)),
    _spec("pulumi", C.CLOUD_CLI, ("version",), probe_env=(("PULUMI_SKIP_UPDATE_CHECK", "true"),)),
    _spec("wrangler", C.CLOUD_CLI, None),
    _spec("firebase", C.CLOUD_CLI, None),
    # -- editors / IDEs (GUI launchers are never started) -------------------
    _spec("code", C.EDITOR_IDE, None),
    _spec("cursor", C.EDITOR_IDE, None),
    _spec("subl", C.EDITOR_IDE, None),
    _spec("vim", C.EDITOR_IDE),
    _spec("nvim", C.EDITOR_IDE),
    _spec("emacs", C.EDITOR_IDE),
    _spec("nano", C.EDITOR_IDE),
    _spec("zed", C.EDITOR_IDE, None),
    _spec("hx", C.EDITOR_IDE),
    _spec("idea", C.EDITOR_IDE, None),
    _spec("pycharm", C.EDITOR_IDE, None),
    _spec("clion", C.EDITOR_IDE, None),
    _spec("rider", C.EDITOR_IDE, None),
    _spec("goland", C.EDITOR_IDE, None),
    _spec("devenv", C.EDITOR_IDE, None, platforms=WIN),
    _spec("xcode", C.EDITOR_IDE, None, executables=(), platforms=MAC),
    # -- shells and core command-line utilities ----------------------------
    _spec("bash", C.SHELL),
    _spec("zsh", C.SHELL),
    _spec("fish", C.SHELL),
    _spec("pwsh", C.SHELL),
    _spec("powershell", C.SHELL, None, platforms=WIN),
    _spec("cmd", C.SHELL, None, platforms=WIN),
    _spec("sh", C.SHELL, None),
    _spec("nssm", C.SHELL, None, platforms=WIN),
    _spec("clcache", C.SHELL),
    _spec("rg", C.SHELL),
    _spec("fd", C.SHELL, executables=("fd", "fdfind")),
    _spec("jq", C.SHELL),
    _spec("curl", C.SHELL),
    _spec("wget", C.SHELL),
    _spec("tar", C.SHELL),
    _spec("7z", C.SHELL, None, executables=("7z", "7za", "7zz")),
    _spec("unzip", C.SHELL, ("-v",)),
)

_BY_NAME = {spec.name.casefold(): spec for spec in HOST_TOOL_SPECS}


def spec_for(name: str) -> ToolSpec | None:
    """Return the registry entry for a tool name (case-insensitive)."""
    if not isinstance(name, str):
        return None
    return _BY_NAME.get(name.strip().casefold())


def specs_for_platform(system: str) -> tuple[ToolSpec, ...]:
    """Registry entries applicable to ``platform.system()`` value *system*."""
    return tuple(spec for spec in HOST_TOOL_SPECS if spec.supports(system))


__all__ = ["HOST_TOOL_SPECS", "spec_for", "specs_for_platform"]
