"""Host-owned tool registry: coverage, uniqueness, never-executed classes, drift."""
from sonder_runtime.domain.host_tools.model import BRIEF_EXCLUDED_NAMES, ToolCategory
from sonder_runtime.domain.host_tools.registry import HOST_TOOL_SPECS, spec_for, specs_for_platform
from sonder_runtime.platform import toolchain_policy

REQUIRED_ANCHORS = {
    ToolCategory.COMPILER: "gcc g++ clang clang++ cl rustc javac kotlinc swiftc zig nvcc gfortran tsc",
    ToolCategory.BUILD_SYSTEM: "cmake ninja make mingw32-make nmake msbuild meson bazel bazelisk scons "
                               "gradle mvn ant sbt cargo dotnet xmake premake5 pkg-config windows-sdk ccache sccache",
    ToolCategory.TEST_RUNNER: "pytest tox nox ctest jest vitest mocha cargo-nextest phpunit rspec",
    ToolCategory.LINTER_FORMATTER: "ruff flake8 pylint black isort mypy pyright eslint prettier biome "
                                   "clang-format clang-tidy cppcheck rustfmt gofmt golangci-lint shellcheck "
                                   "shfmt hadolint yamllint",
    ToolCategory.DEBUGGER_PROFILER: "gdb lldb valgrind perf strace ltrace rr cdb windbg py-spy heaptrack "
                                    "xperf wpaexporter",
    ToolCategory.PACKAGE_MANAGER: "pip pip3 uv pipx poetry conda mamba npm pnpm yarn bun nuget vcpkg conan "
                                  "apt dnf pacman zypper apk brew port winget scoop choco gem bundle composer",
    ToolCategory.RUNTIME: "python python3 py node deno java ruby perl php lua Rscript julia erl elixir go",
    ToolCategory.CONTAINER_VM: "docker podman nerdctl kubectl helm minikube kind vagrant qemu-system-x86_64 "
                               "VBoxManage wsl multipass limactl",
    ToolCategory.VCS: "git gh glab hg svn git-lfs p4",
    ToolCategory.DB_CLIENT: "sqlite3 psql mysql mariadb mongosh redis-cli sqlcmd duckdb",
    ToolCategory.MEDIA_DOC: "ffmpeg ffprobe magick pandoc tesseract gs inkscape blender dot latexmk "
                            "pdflatex qpdf exiftool sox doxygen",
    ToolCategory.CLOUD_CLI: "aws az gcloud doctl flyctl heroku vercel netlify terraform pulumi wrangler firebase",
    ToolCategory.EDITOR_IDE: "code cursor subl vim nvim emacs nano zed hx idea pycharm clion rider goland devenv",
    ToolCategory.SHELL: "bash zsh fish pwsh powershell cmd sh nssm clcache",
}

NEVER_EXECUTED = {
    "idea", "pycharm", "clion", "rider", "goland", "devenv", "blender", "inkscape", "zed", "subl",
    "cl", "link", "wsl", "az", "gcloud", "firebase", "heroku", "vercel", "netlify", "flyctl",
}


def test_every_category_is_populated_with_its_anchors():
    names = {spec.name: spec for spec in HOST_TOOL_SPECS}
    assert len(HOST_TOOL_SPECS) >= 140
    for category, anchors in REQUIRED_ANCHORS.items():
        for anchor in anchors.split():
            assert anchor in names, anchor
            assert names[anchor].category is category, anchor
    assert {spec.category for spec in HOST_TOOL_SPECS} == set(ToolCategory)


def test_names_are_unique():
    names = [spec.name.casefold() for spec in HOST_TOOL_SPECS]
    assert len(names) == len(set(names))


def test_gui_network_and_metadata_tools_are_never_executed():
    for name in NEVER_EXECUTED:
        assert spec_for(name).version_args is None, name
    terraform = spec_for("terraform")
    assert terraform.version_args == ("-version",)
    assert ("CHECKPOINT_DISABLE", "1") in terraform.probe_env


def test_brief_exclusions_are_single_sourced():
    excluded = {spec.name for spec in HOST_TOOL_SPECS if not spec.in_brief}
    assert excluded == set(BRIEF_EXCLUDED_NAMES)
    assert {"ccache", "sccache", "xperf", "wpaexporter", "doxygen", "nssm", "clcache",
            "sonder-infer"} == excluded


def test_version_arguments_are_fixed_literals():
    for spec in HOST_TOOL_SPECS:
        if spec.version_args is None:
            continue
        assert 1 <= len(spec.version_args) <= 2, spec.name
        for arg in spec.version_args:
            assert arg and arg.replace("-", "").isalnum() or arg.startswith("--"), (spec.name, arg)


def test_drift_legacy_version_arguments_match_the_registry():
    for name, arguments in toolchain_policy.VERSION_ARGUMENTS.items():
        spec = spec_for(name)
        assert spec is not None, name
        assert spec.version_args == arguments, name


def test_drift_check_detects_a_planted_mismatch(monkeypatch):
    """Mutation proof: the drift check fails when the legacy table diverges."""
    planted = dict(toolchain_policy.VERSION_ARGUMENTS, git=("version",))
    mismatches = [n for n, a in planted.items() if spec_for(n).version_args != a]
    assert mismatches == ["git"]


def test_platform_filtering():
    linux = {spec.name for spec in specs_for_platform("Linux")}
    windows = {spec.name for spec in specs_for_platform("Windows")}
    assert "strace" in linux and "strace" not in windows
    assert "cl" in windows and "cl" not in linux
    # coreutils ``link`` must never be mistaken for the MSVC linker.
    assert "link" not in linux
    assert "gcc" in linux and "gcc" in windows
