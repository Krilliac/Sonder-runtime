"""Small bounded code runner exposed through the MCP server.

This is intentionally lightweight: it is not a security sandbox. It gives an
agent a Claude-like way to execute short local snippets while keeping the
interface predictable: known languages, workspace-confined cwd, timeout, and
trimmed output.
"""
import os
import glob
import json
import ntpath
import shutil
import subprocess
import sys
import tempfile
import sonder_logging
import sonder_paths
from sonder_runtime.application.workflows import loop as workflow_loop


SUPPORTED_LANGUAGES = {
    "python": {
        "aliases": {"python", "py"},
        "suffix": ".py",
        "cmd": lambda path: [sys.executable, path],
        "missing": "python executable not available",
    },
    "javascript": {
        "aliases": {"javascript", "js", "node"},
        "suffix": ".js",
        "cmd": lambda path: ["node", path],
        "missing": "node executable not found on PATH",
    },
    "powershell": {
        "aliases": {"powershell", "pwsh", "ps1"},
        "suffix": ".ps1",
        "cmd": lambda path: [_powershell_exe(), "-NoProfile", "-File", path],
        "missing": "PowerShell executable not found on PATH",
    },
    "cpp": {
        "aliases": {"cpp", "c++", "cc", "cxx"},
        "suffix": ".cpp",
        "missing": "C++ compiler not found (tried g++, clang++, cl, and Visual Studio vcvars64.bat)",
    },
    "csharp": {
        "aliases": {"csharp", "cs", "c#"},
        "suffix": ".cs",
        "missing": ".NET SDK or C# compiler not found on PATH (tried dotnet, csc)",
    },
    "bash": {
        "aliases": {"bash", "sh", "shell", "zsh"},
        "suffix": ".sh",
        "cmd": lambda path: [sonder_paths.bash_executable() or "bash", path],
        "missing": "bash/sh executable not found on PATH",
    },
    "ruby": {
        "aliases": {"ruby", "rb"},
        "suffix": ".rb",
        "cmd": lambda path: ["ruby", path],
        "missing": "ruby executable not found on PATH",
    },
    "perl": {
        "aliases": {"perl", "pl"},
        "suffix": ".pl",
        "cmd": lambda path: ["perl", path],
        "missing": "perl executable not found on PATH",
    },
    "php": {
        "aliases": {"php"},
        "suffix": ".php",
        "cmd": lambda path: ["php", path],
        "missing": "php executable not found on PATH",
    },
    "lua": {
        "aliases": {"lua"},
        "suffix": ".lua",
        "cmd": lambda path: [shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3") or "lua", path],
        "missing": "lua executable not found on PATH",
    },
    "r": {
        "aliases": {"r", "rscript"},
        "suffix": ".R",
        "cmd": lambda path: ["Rscript", path],
        "missing": "Rscript executable not found on PATH",
    },
    "go": {
        "aliases": {"go", "golang"},
        "suffix": ".go",
        # `go run` compiles and executes in one bounded step.
        "cmd": lambda path: ["go", "run", path],
        "missing": "go toolchain not found on PATH",
    },
    "java": {
        "aliases": {"java"},
        "suffix": ".java",
        # JDK 11+ single-file source launcher; no separate compile step.
        "cmd": lambda path: ["java", path],
        "missing": "java (JDK 11+) not found on PATH",
    },
    "typescript": {
        "aliases": {"typescript", "ts"},
        "suffix": ".ts",
        # Node 22.6+ strips type annotations natively; older nodes report
        # their own actionable error through stderr.
        "cmd": lambda path: ["node", "--experimental-strip-types", path],
        "missing": "node executable not found on PATH (TypeScript runs via node --experimental-strip-types)",
    },
    "rust": {
        "aliases": {"rust", "rs"},
        "suffix": ".rs",
        "missing": "rustc not found on PATH",
    },
}

DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 60
MAX_OUTPUT_CHARS = 12000
DEFAULT_LOOP_ITERATIONS = 5
MAX_LOOP_ITERATIONS = 50
MAX_LOOP_DELAY_SECONDS = 10.0
MAX_PROJECT_FILES = 80
MAX_PROJECT_BYTES = 750000
RUN_WINDOW_DIR_ENV = "SONDER_RUN_WINDOW_DIR"


def _child_environment():
    # Model-authored children used to inherit bypass/API credentials and could
    # print them into tool output, granting themselves control-plane authority.
    # The stripping rule now lives in sonder_logging so this lane and
    # workbench's run_program cannot diverge -- they did, and workbench leaked
    # the whole environment to the same class of model-authored child.
    return sonder_logging.child_environment()


def _powershell_exe():
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def normalize_language(language):
    wanted = (language or "python").strip().lower()
    for canonical, cfg in SUPPORTED_LANGUAGES.items():
        if wanted in cfg["aliases"]:
            return canonical
    raise ValueError(
        "unsupported language %r. Supported: %s"
        % (language, ", ".join(sorted(SUPPORTED_LANGUAGES)))
    )


def resolve_cwd(cwd=None):
    # Resolve symlinks on both the workspace root and the candidate cwd before
    # the containment check (via os.path.realpath), mirroring file_ops.resolve_path
    # which uses Path.resolve(). Without this, a symlink living inside the
    # workspace but pointing outside it would pass an abspath+commonpath check and
    # place the child process's cwd outside the tree. This is a tidiness boundary,
    # not a sandbox (see module docstring); for non-symlinked paths the result is
    # identical to the previous os.path.abspath behavior.
    root = os.path.realpath(workspace_root())
    if not cwd:
        return root
    path = cwd
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    path = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("cwd must stay inside workspace: %r" % cwd)
    if not os.path.isdir(path):
        raise ValueError("cwd does not exist: %r" % cwd)
    return path


def _clamp_timeout(timeout):
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1, min(value, MAX_TIMEOUT))


def _trim_output(text, limit=MAX_OUTPUT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _completed_result(proc, language, cwd, timeout, error=""):
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _trim_output(proc.stdout),
        "stderr": _trim_output(proc.stderr),
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": error,
    }


def _error_result(language, cwd, timeout, error):
    return {
        "ok": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": error,
    }


def _timeout_result(language, cwd, timeout, exc):
    return {
        "ok": False,
        "returncode": None,
        "stdout": _trim_output(exc.stdout if isinstance(exc.stdout, str) else ""),
        "stderr": _trim_output(exc.stderr if isinstance(exc.stderr, str) else ""),
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": "timed out after %ss" % timeout,
    }


def _run_process(cmd, cwd, stdin, timeout, language):
    if language == "bash" and not sonder_paths.bash_executable():
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES[language]["missing"])
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=stdin or "",
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_environment(),
            # On Windows close_fds otherwise defaults to false.  The code is
            # model-authored and can inspect arbitrary inherited handles, so
            # do not hand it a live server pipe, log/database handle, or IPC
            # channel merely because another runtime component opened one as
            # inheritable.  stdin/stdout/stderr above remain the intentional
            # communication boundary.
            close_fds=True,
        )
    except FileNotFoundError:
        missing = SUPPORTED_LANGUAGES.get(language, {}).get(
            "missing", "executable not found: %s" % (cmd[0] if cmd else "(empty)")
        )
        return _error_result(language, cwd, timeout, missing)
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(language, cwd, timeout, exc)
    return _completed_result(proc, language, cwd, timeout)


def _persistent_run_dir():
    base = os.environ.get(RUN_WINDOW_DIR_ENV, "").strip()
    if not base:
        home = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base = os.path.join(home, "sonder", "runs")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="sonder-window-", dir=base)


def _bat_quote(value):
    return '"%s"' % str(value).replace('"', "")


def _write_launcher(run_dir, lines):
    path = os.path.join(run_dir, "launch.bat")
    with open(path, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n")
        f.write("cd /d %s\r\n" % _bat_quote(run_dir))
        for line in lines:
            f.write(line.rstrip() + "\r\n")
    return path


def _launch_console(launcher, cwd, language, timeout):
    if os.name != "nt":
        return _error_result(
            language,
            cwd,
            timeout,
            "/runwindow is only available on Windows consoles",
        )
    try:
        proc = subprocess.Popen(
            ["cmd", "/k", launcher],
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            env=_child_environment(),
            # A detached window is still executing model-authored code.  It
            # has no legitimate reason to inherit the host's other handles.
            close_fds=True,
        )
    except FileNotFoundError:
        return _error_result(language, cwd, timeout, "cmd.exe not found")
    except OSError as exc:
        return _error_result(language, cwd, timeout, str(exc))
    return {
        "ok": True,
        "returncode": None,
        "stdout": "launched in a separate console window (pid %s)" % proc.pid,
        "stderr": "",
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": "",
        "detached": True,
        "pid": proc.pid,
        "run_dir": os.path.dirname(launcher),
    }


def _cpp_compiler():
    for exe in ("g++", "clang++", "cl"):
        path = shutil.which(exe)
        if path:
            return exe, path
    vcvars = _find_visual_studio_vcvars()
    if vcvars:
        return "msvc-vcvars", vcvars
    return None, None


def _find_visual_studio_vcvars():
    override = os.environ.get("SONDER_VCVARS64", "").strip()
    if override and os.path.isfile(override):
        return override

    vswhere = shutil.which("vswhere")
    if not vswhere:
        candidate = os.path.join(
            sonder_paths.windows_program_files(x86=True),
            "Microsoft Visual Studio",
            "Installer",
            "vswhere.exe",
        )
        if os.path.isfile(candidate):
            vswhere = candidate
    if vswhere:
        try:
            proc = subprocess.run(
                [
                    vswhere,
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                env=_child_environment(),
            )
            install = (proc.stdout or "").strip().splitlines()
            if install:
                joiner = ntpath.join if "\\" in install[0] or ":" in install[0] else os.path.join
                vcvars = joiner(install[0], "VC", "Auxiliary", "Build", "vcvars64.bat")
                if os.path.isfile(vcvars):
                    return vcvars
        except (OSError, subprocess.SubprocessError):
            pass

    roots = [
        os.path.join(sonder_paths.windows_program_files(), "Microsoft Visual Studio", "*", "*"),
        os.path.join(sonder_paths.windows_program_files(x86=True), "Microsoft Visual Studio", "*", "*"),
    ]
    for root in roots:
        matches = sorted(
            glob.glob(os.path.join(root, "VC", "Auxiliary", "Build", "vcvars64.bat")),
            reverse=True,
        )
        for path in matches:
            if os.path.isfile(path):
                return path
    return None


def _msvc_compile_result(vcvars, sources, exe, cwd, timeout, language):
    bat = os.path.join(cwd, "sonder_build_msvc.bat")
    quoted_sources = " ".join('"%s"' % src for src in sources)
    with open(bat, "w", encoding="utf-8") as f:
        f.write(
            '@echo off\r\n'
            'call "%s" >nul\r\n'
            'cl /nologo /EHsc /std:c++17 /Fe:"%s" %s\r\n'
            % (vcvars, exe, quoted_sources)
        )
    return _run_process(["cmd", "/c", bat], cwd, "", timeout, language)


def _run_cpp(path, tmp, stdin, timeout, cwd):
    name, compiler = _cpp_compiler()
    if not compiler:
        return _error_result("cpp", cwd, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"])
    exe_name = "snippet.exe" if os.name == "nt" or name in ("msvc-vcvars", "cl") else "snippet"
    exe = os.path.join(tmp, exe_name)
    if name == "msvc-vcvars":
        compile_result = _msvc_compile_result(compiler, [path], exe, tmp, timeout, "cpp")
    elif name == "cl":
        compile_cmd = [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe, path]
        compile_result = _run_process(compile_cmd, tmp, "", timeout, "cpp")
    else:
        compile_cmd = [compiler, "-std=c++17", path, "-o", exe]
        compile_result = _run_process(compile_cmd, tmp, "", timeout, "cpp")
    if not compile_result.get("ok"):
        return compile_result
    run_result = _run_process([exe], cwd, stdin, timeout, "cpp")
    run_result["stdout"] = _trim_output(
        (compile_result.get("stdout") or "") + (("\n" + run_result["stdout"]) if run_result.get("stdout") else "")
    )
    run_result["stderr"] = _trim_output(
        (compile_result.get("stderr") or "") + (("\n" + run_result["stderr"]) if run_result.get("stderr") else "")
    )
    return run_result


def _compile_cpp_for_window(path, run_dir, timeout):
    name, compiler = _cpp_compiler()
    if not compiler:
        return _error_result("cpp", run_dir, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"]), ""
    exe = os.path.join(run_dir, "snippet.exe")
    if name == "msvc-vcvars":
        result = _msvc_compile_result(compiler, [path], exe, run_dir, timeout, "cpp")
    elif name == "cl":
        result = _run_process(
            [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe, path],
            run_dir,
            "",
            timeout,
            "cpp",
        )
    else:
        result = _run_process(
            [compiler, "-std=c++17", path, "-o", exe],
            run_dir,
            "",
            timeout,
            "cpp",
        )
    return result, exe


def _run_rust(path, tmp, stdin, timeout, cwd):
    rustc = shutil.which("rustc")
    if not rustc:
        return _error_result(
            "rust", cwd, timeout, SUPPORTED_LANGUAGES["rust"]["missing"]
        )
    exe = os.path.join(tmp, "snippet.exe" if os.name == "nt" else "snippet.bin")
    compile_result = _run_process(
        [rustc, "-O", "--edition", "2021", path, "-o", exe],
        tmp, "", timeout, "rust",
    )
    if not compile_result["ok"]:
        compile_result["error"] = compile_result.get("error") or "rust compilation failed"
        return compile_result
    return _run_process([exe], cwd, stdin, timeout, "rust")


def _run_csharp(path, tmp, stdin, timeout, cwd):
    csc = shutil.which("csc")
    if csc:
        exe = os.path.join(tmp, "snippet.exe")
        compile_result = _run_process([csc, "/nologo", "/out:" + exe, path], tmp, "", timeout, "csharp")
        if not compile_result.get("ok"):
            return compile_result
        return _run_process([exe], cwd, stdin, timeout, "csharp")
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return _error_result("csharp", cwd, timeout, SUPPORTED_LANGUAGES["csharp"]["missing"])
    project = os.path.join(tmp, "Snippet.csproj")
    with open(project, "w", encoding="utf-8") as f:
        f.write(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <OutputType>Exe</OutputType>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '    <Nullable>enable</Nullable>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        )
    os.replace(path, os.path.join(tmp, "Program.cs"))
    return _run_process([dotnet, "run", "--project", project], cwd, stdin, timeout, "csharp")


def _compile_csharp_for_window(path, run_dir, timeout):
    csc = shutil.which("csc")
    if csc:
        exe = os.path.join(run_dir, "snippet.exe")
        result = _run_process([csc, "/nologo", "/out:" + exe, path], run_dir, "", timeout, "csharp")
        return result, [_bat_quote(exe)]
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return _error_result("csharp", run_dir, timeout, SUPPORTED_LANGUAGES["csharp"]["missing"]), []
    program = os.path.join(run_dir, "Program.cs")
    os.replace(path, program)
    project = os.path.join(run_dir, "Snippet.csproj")
    with open(project, "w", encoding="utf-8") as f:
        f.write(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <OutputType>Exe</OutputType>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '    <Nullable>enable</Nullable>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        )
    result = _run_process([dotnet, "build", project, "--nologo", "-v:q"], run_dir, "", timeout, "csharp")
    return result, [_bat_quote(dotnet), "run", "--project", _bat_quote(project), "--no-build"]


def _clamp_iterations(max_iterations):
    return workflow_loop.clamp_iterations(max_iterations)


def _clamp_delay(delay_seconds):
    return workflow_loop.clamp_delay(delay_seconds)


def run_code(code, language="python", stdin="", timeout=DEFAULT_TIMEOUT, cwd=None):
    """Run a snippet and return a result dict.

    The caller receives:
      ok: process exited 0
      returncode: int or None on timeout/unavailable
      stdout/stderr: trimmed process streams
      language/cwd/timeout: resolved execution metadata
      error: runner-level error text, when applicable
    """
    if not (code or "").strip():
        raise ValueError("code is empty")
    language = normalize_language(language)
    timeout = _clamp_timeout(timeout)
    cfg = SUPPORTED_LANGUAGES[language]

    with tempfile.TemporaryDirectory(prefix="sonder-run-") as tmp:
        # The ungated default previously executed beside the runtime's own
        # control-plane modules, making a relative write a live self-modification.
        cwd = resolve_cwd(cwd) if cwd else os.path.realpath(tmp)
        path = os.path.join(tmp, "snippet" + cfg["suffix"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        if language == "cpp":
            return _run_cpp(path, tmp, stdin, timeout, cwd)
        if language == "csharp":
            return _run_csharp(path, tmp, stdin, timeout, cwd)
        if language == "rust":
            return _run_rust(path, tmp, stdin, timeout, cwd)
        cmd = cfg["cmd"](path)
        return _run_process(cmd, cwd, stdin, timeout, language)


def run_code_window(code, language="python", timeout=DEFAULT_TIMEOUT, cwd=None):
    """Compile/write a snippet and launch it in a separate Windows console.

    Unlike run_code(), this deliberately keeps the generated run directory so an
    interactive console app or compiled executable stays available after launch.
    """
    if not (code or "").strip():
        raise ValueError("code is empty")
    language = normalize_language(language)
    timeout = _clamp_timeout(timeout)
    if os.name != "nt":
        cwd = resolve_cwd(cwd) if cwd else os.path.realpath(tempfile.gettempdir())
        return _error_result(language, cwd, timeout, "/runwindow is only available on Windows consoles")

    cfg = SUPPORTED_LANGUAGES[language]
    run_dir = _persistent_run_dir()
    cwd = resolve_cwd(cwd) if cwd else os.path.realpath(run_dir)
    path = os.path.join(run_dir, "snippet" + cfg["suffix"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    if language == "cpp":
        compile_result, exe = _compile_cpp_for_window(path, run_dir, timeout)
        if not compile_result.get("ok"):
            compile_result["run_dir"] = run_dir
            return compile_result
        launcher = _write_launcher(run_dir, [_bat_quote(exe)])
        return _launch_console(launcher, run_dir, language, timeout)

    if language == "csharp":
        compile_result, command = _compile_csharp_for_window(path, run_dir, timeout)
        if not compile_result.get("ok"):
            compile_result["run_dir"] = run_dir
            return compile_result
        launcher = _write_launcher(run_dir, [" ".join(command)])
        return _launch_console(launcher, run_dir, language, timeout)

    if language == "javascript" and not shutil.which("node"):
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["javascript"]["missing"])
    if language == "powershell" and not (shutil.which("pwsh") or shutil.which("powershell")):
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["powershell"]["missing"])

    cmd = cfg["cmd"](path)
    launcher = _write_launcher(run_dir, [" ".join(_bat_quote(part) for part in cmd)])
    return _launch_console(launcher, run_dir, language, timeout)


def format_result(result):
    status = "ok" if result.get("ok") else "failed"
    rc = result.get("returncode")
    lines = [
        "status: %s" % status,
        "language: %s" % result.get("language"),
        "cwd: %s" % result.get("cwd"),
        "timeout: %ss" % result.get("timeout"),
        "returncode: %s" % ("(none)" if rc is None else rc),
    ]
    if result.get("error"):
        lines.append("error: %s" % result["error"])
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if "EOFError" in stderr:
        lines.append(
            "note: this program tried to read keyboard input, but /run is non-interactive."
        )
    if (
        result.get("error", "").startswith("timed out")
        and result.get("language") in ("csharp", "cpp", "project")
        and stdout
        and ("enter" in stdout.lower() or "guess" in stdout.lower() or "input" in stdout.lower())
    ):
        lines.append(
            "note: the program appears to be waiting for console input. For /run, add a scripted demo path or provide stdin."
        )
    if result.get("error", "").startswith("timed out"):
        lines.append("note: use a bounded smoke test or auto-exit path for /run.")
    if stdout:
        lines.extend(["", "stdout:", stdout])
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not stdout and not stderr and not result.get("error"):
        lines.append("")
        lines.append("(no output)")
    return "\n".join(lines)


def format_window_result(result):
    lines = [format_result(result)]
    if result.get("detached"):
        lines.append("run dir: %s" % result.get("run_dir"))
        lines.append("note: close the launched console window when you are done.")
    elif result.get("run_dir"):
        lines.append("run dir: %s" % result.get("run_dir"))
    return "\n".join(line for line in lines if line)


def _project_files_from_json(files_json):
    try:
        parsed = json.loads(files_json) if isinstance(files_json, str) else files_json
    except json.JSONDecodeError as exc:
        raise ValueError("files_json is not valid JSON: %s" % exc)
    if isinstance(parsed, dict) and "files" in parsed:
        parsed = parsed["files"]
    if isinstance(parsed, dict):
        files = [{"path": path, "content": content} for path, content in parsed.items()]
    elif isinstance(parsed, list):
        files = parsed
    else:
        raise ValueError("files_json must be a dict of path->content or a list of file objects")
    if not files:
        raise ValueError("project has no files")
    if len(files) > MAX_PROJECT_FILES:
        raise ValueError("too many project files (max %d)" % MAX_PROJECT_FILES)
    total = 0
    clean = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each project file must be an object")
        path = str(item.get("path") or "").replace("\\", "/").strip()
        content = item.get("content")
        if not path or content is None:
            raise ValueError("each project file needs path and content")
        if os.path.isabs(path) or path.startswith("../") or "/../" in path or path == "..":
            raise ValueError("unsafe project path: %r" % path)
        total += len(str(content).encode("utf-8"))
        if total > MAX_PROJECT_BYTES:
            raise ValueError("project content too large (max %d bytes)" % MAX_PROJECT_BYTES)
        clean.append({"path": path, "content": str(content)})
    return clean


def _write_project_files(root, files):
    for item in files:
        dest = os.path.abspath(os.path.join(root, item["path"]))
        try:
            inside = os.path.commonpath([root, dest]) == root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("unsafe project path: %r" % item["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(item["content"])


def _commands_from_json(commands_json):
    if not commands_json:
        return None
    try:
        parsed = json.loads(commands_json) if isinstance(commands_json, str) else commands_json
    except json.JSONDecodeError as exc:
        raise ValueError("commands_json is not valid JSON: %s" % exc)
    if isinstance(parsed, dict) and "commands" in parsed:
        parsed = parsed["commands"]
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("commands_json must be a non-empty list")
    commands = []
    for item in parsed:
        if isinstance(item, list):
            cmd, cwd = item, ""
        elif isinstance(item, dict):
            cmd, cwd = item.get("cmd"), item.get("cwd", "")
        else:
            raise ValueError("each command must be an argv list or object")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
            raise ValueError("command cmd must be a non-empty argv string list")
        commands.append({"cmd": cmd, "cwd": str(cwd or "")})
    return commands


def _project_cwd(root, rel):
    path = os.path.abspath(os.path.join(root, rel or ""))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isdir(path):
        raise ValueError("command cwd must stay inside project: %r" % rel)
    return path


def _auto_project_commands(root, files):
    paths = [f["path"] for f in files]
    lower = [p.lower() for p in paths]
    py_main = next((p for p in paths if p.lower() in ("main.py", "app.py")), None)
    if py_main:
        return [{"cmd": [sys.executable, py_main], "cwd": ""}]
    csproj = next((p for p in paths if p.lower().endswith(".csproj")), None)
    if csproj:
        return [{"cmd": ["dotnet", "run", "--project", csproj], "cwd": ""}]
    cs_files = [p for p in paths if p.lower().endswith(".cs")]
    if cs_files:
        project = os.path.join(root, "SonderGenerated.csproj")
        with open(project, "w", encoding="utf-8") as f:
            f.write(
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                '  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n'
                '</Project>\n'
            )
        return [{"cmd": ["dotnet", "run", "--project", "SonderGenerated.csproj"], "cwd": ""}]
    cpp_files = [p for p in paths if p.lower().endswith((".cpp", ".cc", ".cxx"))]
    if cpp_files:
        name, compiler = _cpp_compiler()
        if not compiler:
            return [{"cmd": ["__missing_cpp_compiler__"], "cwd": ""}]
        exe = os.path.abspath(os.path.join(root, "app.exe" if os.name == "nt" else "app"))
        if name == "msvc-vcvars":
            sources = [os.path.abspath(os.path.join(root, p)) for p in cpp_files]
            bat = os.path.join(root, "sonder_build_msvc.bat")
            quoted_sources = " ".join('"%s"' % src for src in sources)
            with open(bat, "w", encoding="utf-8") as f:
                f.write(
                    '@echo off\r\n'
                    'call "%s" >nul\r\n'
                    'cl /nologo /EHsc /std:c++17 /Fe:"%s" %s\r\n'
                    % (compiler, exe, quoted_sources)
                )
            compile_cmd = ["cmd", "/c", bat]
        elif name == "cl":
            compile_cmd = [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe] + cpp_files
        else:
            compile_cmd = [compiler, "-std=c++17"] + cpp_files + ["-o", exe]
        return [{"cmd": compile_cmd, "cwd": ""}, {"cmd": [exe], "cwd": ""}]
    if "package.json" in lower:
        return [{"cmd": ["npm", "test"], "cwd": ""}]
    raise ValueError("could not auto-detect how to run project; provide commands_json")


def run_project(files_json, commands_json="", stdin="", timeout=MAX_TIMEOUT):
    files = _project_files_from_json(files_json)
    commands = _commands_from_json(commands_json)
    timeout = _clamp_timeout(timeout)
    with tempfile.TemporaryDirectory(prefix="sonder-project-") as root:
        root = os.path.abspath(root)
        _write_project_files(root, files)
        if commands is None:
            commands = _auto_project_commands(root, files)
        steps = []
        ok = True
        for index, command in enumerate(commands, start=1):
            cwd = _project_cwd(root, command.get("cwd", ""))
            cmd = command["cmd"]
            language = "project"
            if cmd and cmd[0] == "__missing_cpp_compiler__":
                result = _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"])
            else:
                try:
                    result = _run_process(cmd, cwd, stdin if index == len(commands) else "", timeout, language)
                except ValueError as exc:
                    result = _error_result(language, cwd, timeout, str(exc))
            steps.append({"index": index, "cmd": cmd, "cwd": cwd, "result": result})
            if not result.get("ok"):
                ok = False
                break
    return {"ok": ok, "files": [f["path"] for f in files], "steps": steps, "timeout": timeout}


def format_project_result(result):
    lines = [
        "project status: %s" % ("ok" if result.get("ok") else "failed"),
        "files: %s" % ", ".join(result.get("files") or []),
        "timeout: %ss" % result.get("timeout"),
    ]
    for step in result.get("steps") or []:
        lines.append("")
        lines.append("step %d: %s" % (step["index"], " ".join(step.get("cmd") or [])))
        formatted = format_result(step["result"])
        for line in formatted.splitlines():
            lines.append("  " + line)
    return "\n".join(lines)


def run_loop(
    actions,
    dispatch_action,
    max_iterations=DEFAULT_LOOP_ITERATIONS,
    stop_on_failure=True,
    stop_on_success=False,
    delay_seconds=0,
    cancel_check=None,
):
    """Compatibility delegate to the package application use case."""
    return workflow_loop.run_loop(
        actions,
        dispatch_action,
        max_iterations=max_iterations,
        stop_on_failure=stop_on_failure,
        stop_on_success=stop_on_success,
        delay_seconds=delay_seconds,
        cancel_check=cancel_check,
    )


def format_loop_result(loop_result):
    return workflow_loop.format_loop_result(loop_result)
