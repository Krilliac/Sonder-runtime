"""Guarded adapter for ``learn_tool`` (/learntool).

Allows Sonder to autonomously discover and figure out unfamiliar external tools
(e.g. Blender, Inkscape, FFmpeg, Krita, Godot, Pandoc, ImageMagick, or any
unknown CLI/Python package) through:
1. Safe read-only binary discovery (PATH, Windows App Paths, bounded standard
   install folders).
2. Fixed-flag, non-interactive CLI introspection (`--version`, `--help`, or
   `<subcommand> --help`) with `shell=False`, `CREATE_NO_WINDOW`, tight timeout,
   and prompt-injection/secret sanitization.
3. Read-only Python package metadata inspection (`importlib.metadata`).
4. Optional local install-dir README snippet extraction (bounded & sanitized).
5. Optional policy-gated web research & persistent memory recall/save callbacks.
"""
from __future__ import annotations

import importlib.metadata as _metadata
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from sonder_runtime.domain.prompt_defense import scan_untrusted_text
from sonder_runtime.domain.tool_learning import (
    NormalizedToolQuery,
    parse_cli_help,
    synthesize_tool_card,
    validate_subcommand,
    validate_tool_query,
)

MAX_PROBE_BYTES = 65_536
MAX_DOC_BYTES = 32_768
DEFAULT_PROBE_TIMEOUT = 6.0

# Closed allowlist of well-known CLI tools safe to probe with fixed --version/--help
# flags. Arbitrary untrusted binaries on PATH are never spawned automatically.
_SAFE_PROBE_BINARIES = frozenset({
    "python", "python3", "py", "pytest", "pip", "pip3", "uv", "ruff", "mypy", "flake8", "black",
    "git", "gh", "cmake", "ctest", "ninja", "make", "meson", "bazel", "scons",
    "cargo", "rustc", "rustfmt", "clippy-driver", "go", "gofmt", "zig",
    "node", "npm", "npx", "pnpm", "yarn", "deno", "bun", "tsc",
    "clang", "clang++", "clang-format", "clang-tidy", "gcc", "g++", "gdb", "lldb",
    "dotnet", "dart", "flutter", "java", "javac", "mvn", "gradle", "swift", "mix",
    "docker", "podman", "kubectl", "helm", "terraform", "sqlite3", "duckdb",
    "rg", "ripgrep", "fd", "jq", "yq", "curl", "tar", "7z", "7za",
    "blender", "inkscape", "krita", "krita_cli", "audacity", "ffmpeg", "ffprobe",
    "magick", "godot", "godot4", "gimp", "gimp-console", "pandoc",
    "soffice", "libreoffice", "code", "dot", "neato", "yt-dlp",
})
_DENIED_PROBE_BINARIES = frozenset({
    "cmd", "powershell", "pwsh", "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "wsl",
    "shutdown", "reboot", "halt", "poweroff", "format", "diskpart", "bcdedit",
    "reg", "regedit", "net", "net1", "netsh", "sc", "schtasks", "wmic", "certutil",
    "mshta", "rundll32", "regsvr32", "cscript", "wscript", "msiexec", "taskkill",
    "rm", "rmdir", "del", "erase", "dd", "mkfs", "sudo", "su", "chmod", "chown",
    "kill", "pkill", "killall", "convert",  # Windows System32\convert.exe converts filesystems
})
_DISALLOWED_PROBE_SCRIPT_SUFFIXES = frozenset({
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".scr",
})
_PROBE_ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS", "LANG", "LC_ALL", "TEMP", "TMP", "TMPDIR",
})


def _safe_search_path() -> str:
    """Return PATH with empty / relative / CWD entries stripped so CWD binaries are never shadowed."""
    raw = os.environ.get("PATH", "")
    try:
        cwd_norm = os.path.normcase(str(Path.cwd().resolve()))
    except OSError:
        cwd_norm = ""
    kept: list[str] = []
    for entry in raw.split(os.pathsep):
        cleaned = entry.strip().strip('"')
        if not cleaned or cleaned in (".", "./", ".\\"):
            continue
        p = Path(cleaned)
        if not p.is_absolute():
            continue
        try:
            resolved = os.path.normcase(str(p.resolve()))
        except OSError:
            continue
        if cwd_norm and resolved == cwd_norm:
            continue
        kept.append(cleaned)
    return os.pathsep.join(kept)


def _canonical_binary_stem(path: Path) -> str:
    stem = path.stem.lower()
    # Strip trailing version suffixes like python3.12 -> python3, gimp-console-2.10 -> gimp-console
    base = re.sub(r"[-_]?v?\d+(?:\.\d+)*$", "", stem)
    return base or stem


def _is_safe_probe_binary(binary_path: str) -> tuple[bool, str]:
    """Verify *binary_path* is an allowlisted, non-CWD executable safe for --help probing.

    Standard OS PATH symlinks (e.g. ``/usr/bin/python -> python3.14`` on Linux/macOS)
    are permitted only when BOTH the link name and its resolved target are regular
    files outside CWD, not script wrappers, not denied, and on the safe allowlist.
    """
    p = Path(binary_path)
    try:
        resolved = p.resolve(strict=True)
    except OSError:
        return False, "symlink_or_missing"
    if not resolved.is_file():
        return False, "symlink_or_missing"
    if (
        p.suffix.lower() in _DISALLOWED_PROBE_SCRIPT_SUFFIXES
        or resolved.suffix.lower() in _DISALLOWED_PROBE_SCRIPT_SUFFIXES
    ):
        return False, "script_wrapper_refused"
    try:
        cwd_resolved = Path.cwd().resolve()
        if resolved.is_relative_to(cwd_resolved):
            return False, "cwd_binary_refused"
        if p.is_symlink() and p.parent.resolve().is_relative_to(cwd_resolved):
            return False, "cwd_binary_refused"
    except OSError:
        return False, "unresolvable_path"
    for candidate in (p, resolved):
        stem = candidate.stem.lower()
        base_stem = _canonical_binary_stem(candidate)
        if stem in _DENIED_PROBE_BINARIES or base_stem in _DENIED_PROBE_BINARIES:
            return False, "denied_binary"
        if stem not in _SAFE_PROBE_BINARIES and base_stem not in _SAFE_PROBE_BINARIES:
            return False, "unlisted_binary"
    return True, ""


def _sanitized_probe_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, val in os.environ.items():
        if key.upper() in _PROBE_ENV_ALLOWLIST:
            env[key.upper() if os.name == "nt" else key] = val
    env["PATH"] = _safe_search_path()
    return env


def _discover_windows_app_paths(candidates: tuple[str, ...]) -> list[dict[str, str]]:
    if sys.platform != "win32":
        return []
    found: list[dict[str, str]] = []
    try:
        import winreg
    except ImportError:
        return found

    subkey_base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive_name, hive in (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER)):
        for cand in candidates:
            exe_name = cand if cand.lower().endswith(".exe") else f"{cand}.exe"
            try:
                with winreg.OpenKey(hive, f"{subkey_base}\\{exe_name}") as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    p = Path(str(val).strip().strip('"'))
                    if p.is_file() and not p.is_symlink():
                        found.append({"name": p.name, "path": str(p), "source": f"win_app_paths:{hive_name}"})
            except OSError:
                continue
    return found


def _discover_windows_install_dirs(query: NormalizedToolQuery) -> list[dict[str, str]]:
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env_key, "").strip()
        if v:
            roots.append(Path(v))
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        roots.append(Path(local_app) / "Programs")
        roots.append(Path(local_app) / "Microsoft" / "WinGet" / "Links")
    user_prof = os.environ.get("USERPROFILE", "").strip()
    if user_prof:
        roots.append(Path(user_prof) / "scoop" / "shims")
    for extra in (
        Path(r"D:\Program Files"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common"),
        Path(r"D:\SteamLibrary\steamapps\common"),
        Path(r"C:\ProgramData\chocolatey\bin"),
    ):
        if extra.is_dir():
            roots.append(extra)

    folder_needles = tuple(h.lower() for h in query.folder_hints if h)
    exe_stems = {b.lower() for b in query.candidate_binaries}
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for base in roots:
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for top in entries[:400]:
            if not top.is_dir() or top.is_symlink():
                continue
            top_low = top.name.lower()
            if not any(needle in top_low for needle in folder_needles):
                continue
            # Bounded walk (depth <= 2 inside matched install folder, e.g. Blender 4.3\blender.exe or Inkscape\bin\inkscape.exe)
            queue: list[tuple[Path, int]] = [(top, 0)]
            scanned = 0
            while queue and scanned < 150 and len(found) < 6:
                cur, depth = queue.pop(0)
                try:
                    children = list(cur.iterdir())
                except OSError:
                    continue
                for child in children:
                    scanned += 1
                    if child.is_symlink():
                        continue
                    if child.is_file() and child.suffix.lower() in (".exe", ".cmd", ".bat"):
                        stem = child.stem.lower()
                        if stem in exe_stems or any(stem.startswith(s) and len(stem) <= len(s) + 12 for s in exe_stems):
                            norm = str(child.resolve()).lower()
                            if norm not in seen:
                                seen.add(norm)
                                found.append({"name": child.name, "path": str(child.resolve()), "source": "install_dir"})
                    elif child.is_dir() and depth < 2:
                        queue.append((child, depth + 1))
    return found


def discover_tool_binaries(query: NormalizedToolQuery) -> list[dict[str, str]]:
    """Safely locate installed binaries for `query` across PATH, App Paths, and standard install dirs."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    safe_path = _safe_search_path()
    for cand in query.candidate_binaries:
        hit = shutil.which(cand, path=safe_path)
        if hit:
            p = Path(hit)
            try:
                resolved = p.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_file():
                continue
            norm = str(resolved).lower()
            if norm not in seen:
                seen.add(norm)
                results.append({"name": p.name, "path": str(p), "source": "PATH"})

    for row in _discover_windows_app_paths(query.candidate_binaries):
        norm = row["path"].lower()
        if norm not in seen:
            seen.add(norm)
            results.append(row)

    if not results:
        for row in _discover_windows_install_dirs(query):
            norm = row["path"].lower()
            if norm not in seen:
                seen.add(norm)
                results.append(row)

    # Sort highest versioned install folder first if multiple (e.g. Blender 4.3 over 4.1)
    if len(results) > 1 and results[0]["source"] == "install_dir":
        results.sort(key=lambda r: r["path"], reverse=True)
    return results[:6]


def _run_fixed_probe(binary_path: str, argv: list[str], *, timeout: float) -> tuple[bool, str, bool]:
    """Run a single fixed non-interactive read-only probe (`shell=False`, no window, bounded output)."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": max(1.0, min(12.0, float(timeout))),
        "shell": False,
        "cwd": tempfile.gettempdir(),
        "env": _sanitized_probe_env(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        proc = subprocess.run([binary_path, *argv], **kwargs)
        raw_out = (proc.stdout or b"")[:MAX_PROBE_BYTES]
        raw_err = (proc.stderr or b"")[:MAX_PROBE_BYTES]
        text_out = raw_out.decode("utf-8", errors="replace").strip()
        text_err = raw_err.decode("utf-8", errors="replace").strip()
        combined = text_out if len(text_out) >= len(text_err) else (f"{text_out}\n{text_err}".strip() if text_out else text_err)
        return proc.returncode == 0 or bool(combined), combined[:MAX_PROBE_BYTES], False
    except subprocess.TimeoutExpired:
        return False, "", True
    except OSError as exc:
        return False, f"OS error probing binary: {exc}", False


def _probe_binary_cli(
    binary_path: str,
    *,
    subcommand: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> tuple[str, str, str]:
    """Safely probe version and help text from an installed executable.

    Returns ``(probe_status, version_text, help_text)``.
    """
    version_text = ""
    help_text = ""

    # 1. Version probe (non-interactive fixed switches only)
    for flag in ("--version", "-V", "-version"):
        ok, out, timed_out = _run_fixed_probe(binary_path, [flag], timeout=min(timeout, 4.0))
        if timed_out:
            return "timed_out_gui_or_non_cli", "", ""
        if ok and out and "unknown option" not in out.lower() and "unrecognized" not in out.lower():
            version_text = out[:2000]
            break

    # 2. Help probe (root or specific validated subcommand)
    help_argvs: list[list[str]] = (
        [[subcommand, "--help"], [subcommand, "-h"], ["help", subcommand]]
        if subcommand
        else [["--help"], ["-h"]]
    )
    for argv in help_argvs:
        ok, out, timed_out = _run_fixed_probe(binary_path, argv, timeout=timeout)
        if timed_out:
            return "timed_out_gui_or_non_cli", version_text, ""
        if ok and len(out) >= 24:
            help_text = out
            break

    status = "ok" if (help_text or version_text) else "no_cli_help_output"
    return status, version_text, help_text


def _inspect_python_package(candidates: tuple[str, ...]) -> dict[str, Any] | None:
    """Read-only `importlib.metadata` lookup (never imports/executes untrusted code)."""
    for pkg in candidates:
        try:
            dist = _metadata.distribution(pkg)
            meta = dist.metadata
            eps = [ep.name for ep in dist.entry_points if ep.group == "console_scripts"][:10]
            return {
                "installed": True,
                "name": meta.get("Name") or pkg,
                "version": dist.version,
                "summary": str(meta.get("Summary") or "")[:240],
                "home_page": str(meta.get("Home-page") or "")[:200],
                "console_scripts": eps,
            }
        except _metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
    return None


def _read_local_install_docs(binary_path: str) -> list[dict[str, str]]:
    """Read tiny README/doc excerpts next to an installed binary, sanitized against injection."""
    docs: list[dict[str, str]] = []
    try:
        parent = Path(binary_path).resolve().parent
        search_dirs = [parent]
        if parent.name.lower() in ("bin", "scripts") and parent.parent.is_dir():
            search_dirs.append(parent.parent)
        for d in search_dirs:
            for name in ("README.md", "README.txt", "readme.txt", "README", "USAGE.md", "USAGE.txt"):
                cand = d / name
                if cand.is_file() and not cand.is_symlink() and cand.stat().st_size <= MAX_DOC_BYTES:
                    raw = cand.read_text(encoding="utf-8", errors="replace")[:2400]
                    sanitized = str(scan_untrusted_text(raw, redact_secrets=True)["sanitized_text"])
                    docs.append({"file": cand.name, "excerpt": sanitized[:1200]})
                    if len(docs) >= 2:
                        return docs
    except OSError:
        pass
    return docs


def learn_tool(
    tool: str,
    *,
    goal: str = "",
    subcommand: str = "",
    probe_cli: bool = True,
    research_web: bool = False,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    web_search_fn: Callable[[str], Any] | None = None,
    memory_search_fn: Callable[[str], list[str]] | None = None,
) -> dict[str, Any]:
    """Discover, safely introspect, and research an external/unfamiliar tool into a Tool Knowledge Card."""
    query, err = validate_tool_query(tool)
    if query is None:
        return {"ok": False, "error": err}

    clean_sub, sub_err = validate_subcommand(subcommand)
    if sub_err:
        return {"ok": False, "error": sub_err}

    discovered = discover_tool_binaries(query)
    probe_status = "not_installed"
    version_text = ""
    help_text = ""
    local_docs: list[dict[str, str]] = []

    if discovered and probe_cli:
        primary_path = discovered[0]["path"]
        safe_ok, refuse_reason = _is_safe_probe_binary(primary_path)
        if safe_ok:
            probe_status, raw_ver, raw_help = _probe_binary_cli(
                primary_path, subcommand=clean_sub, timeout=timeout,
            )
            # Sanitize all third-party CLI output against secrets & indirect prompt injection
            version_text = str(scan_untrusted_text(raw_ver, redact_secrets=True)["sanitized_text"]) if raw_ver else ""
            help_text = str(scan_untrusted_text(raw_help, redact_secrets=True)["sanitized_text"]) if raw_help else ""
        else:
            probe_status = f"probe_refused:{refuse_reason}"
        local_docs = _read_local_install_docs(primary_path)
    elif discovered:
        probe_status = "skipped_probe"

    parsed = parse_cli_help(help_text, version_text=version_text, goal=goal)
    py_pkg = _inspect_python_package(query.python_packages)

    # Recall previously learned Sonder notes if callback supplied
    memory_notes: list[str] = []
    if memory_search_fn is not None:
        try:
            raw_notes = memory_search_fn(f"{query.canonical} {goal}".strip())
            for note in (raw_notes or [])[:5]:
                clean_note = str(scan_untrusted_text(str(note), redact_secrets=True)["sanitized_text"])[:320]
                if clean_note:
                    memory_notes.append(clean_note)
        except Exception:
            pass

    # Optional policy-gated web research (only when explicitly requested AND host callback provided)
    web_results: list[dict[str, str]] = []
    if research_web and web_search_fn is not None:
        q_str = f"{query.raw} CLI command line flags usage {clean_sub} {goal}".strip()
        try:
            raw_web = web_search_fn(q_str)
            if isinstance(raw_web, str) and raw_web.strip():
                safe_web = str(scan_untrusted_text(raw_web, redact_secrets=True)["sanitized_text"])[:2400]
                web_results.append({"query": q_str, "snippet": safe_web})
            elif isinstance(raw_web, list):
                for item in raw_web[:4]:
                    if isinstance(item, dict):
                        title = str(scan_untrusted_text(str(item.get("title") or ""), redact_secrets=True)["sanitized_text"])[:160]
                        snippet = str(scan_untrusted_text(str(item.get("snippet") or item.get("summary") or ""), redact_secrets=True)["sanitized_text"])[:400]
                        url = str(item.get("url") or "")[:240]
                        web_results.append({"title": title, "url": url, "snippet": snippet})
        except Exception as exc:
            web_results.append({"query": q_str, "error": str(exc)[:180]})

    return synthesize_tool_card(
        query,
        goal=goal,
        subcommand=clean_sub,
        discovered_binaries=discovered,
        probe_status=probe_status,
        parsed_help=parsed,
        python_package=py_pkg,
        local_docs=local_docs,
        memory_notes=memory_notes,
        web_research=web_results,
    )


def maybe_save_playbook_note(
    data: dict[str, Any],
    *,
    save_note: bool,
    in_plan_mode: bool,
    decide_fn: Callable[[str, dict[str, Any]], Any],
    remember_fn: Callable[..., Any],
) -> None:
    """Persist ``playbook_summary`` only when permitted outside read-only plan mode."""
    if not (save_note and data.get("ok") and data.get("playbook_summary")):
        return
    if in_plan_mode:
        data["saved_to_memory"] = False
        data["save_note_error"] = "refused save_note in read-only plan mode"
        return
    decision = decide_fn("sonder_remember_fact", {"text": str(data["playbook_summary"])})
    if not decision.allowed:
        data["saved_to_memory"] = False
        data["save_note_error"] = "refused save_note by permission policy: %s" % (
            decision.reason or decision.action
        )
        return
    try:
        remember_fn(text=str(data["playbook_summary"]))
        data["saved_to_memory"] = True
    except Exception as exc:
        data["saved_to_memory"] = False
        data["save_note_error"] = str(exc)[:160]
