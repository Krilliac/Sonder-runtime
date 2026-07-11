"""One-click local engine bootstrap for bundled Trilobite installs.

The lightweight path uses host Python/Ollama and may download missing pieces.
A sealed platform engine bundle supplies Python, Ollama, and model weights for a
strictly offline setup. Both paths create the stable ``trilobite`` alias.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import engine_bundle


MODEL_SMALL = "qwen2.5-coder:1.5b"
MODEL_MEDIUM = "qwen2.5-coder:3b"
MODEL_LARGE = "qwen2.5-coder:7b"
ROOT = Path(__file__).resolve().parent


def _run(cmd, check=False, env=None, cwd=None, **kwargs):
    print("+ " + " ".join(str(item) for item in cmd))
    return subprocess.run(cmd, check=check, env=env, cwd=cwd, **kwargs)


def total_ram_gb():
    override = os.environ.get("TRILOBITE_RAM_GB", "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
                ],
                text=True,
                timeout=10,
            ).strip()
            return int(out) / (1024**3)
        except Exception:
            return 0.0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024**2)
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
                text=True,
                timeout=10,
            ).strip()
            return int(out) / (1024**3)
        except Exception:
            return 0.0
    return 0.0


def choose_model(ram_gb=None):
    forced = os.environ.get("TRILOBITE_BASE_MODEL", "").strip()
    if forced:
        return forced
    ram = total_ram_gb() if ram_gb is None else float(ram_gb)
    if ram >= 8:
        return MODEL_LARGE
    if ram >= 4:
        return MODEL_MEDIUM
    return MODEL_SMALL


def _ollama_executable(explicit: str = "") -> str:
    candidate = explicit.strip() or os.environ.get("TRILOBITE_OLLAMA_EXE", "").strip()
    if candidate:
        return candidate
    return shutil.which("ollama") or ""


def ensure_ollama_running(
    ollama: str = "",
    *,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    executable = _ollama_executable(ollama)
    if not executable:
        return False, "Ollama is not installed, on PATH, or present in an engine bundle."
    process_env = env or os.environ.copy()
    try:
        probe = subprocess.run(
            [executable, "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not execute Ollama: {exc}"
    if probe.returncode == 0:
        return True, "Ollama is already running."
    print("Starting Ollama...")
    popen_kwargs = {"env": process_env}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        subprocess.Popen([executable, "serve"], **popen_kwargs)
    except OSError as exc:
        return False, f"Could not start Ollama: {exc}"
    for _ in range(30):
        time.sleep(0.5)
        try:
            probe = subprocess.run(
                [executable, "list"],
                capture_output=True,
                text=True,
                timeout=5,
                env=process_env,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return True, "Ollama started."
    return False, "Ollama did not become reachable after startup."


def ensure_python_deps(
    python_executable: str | os.PathLike[str] | None = None,
    *,
    offline: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    executable = str(python_executable or sys.executable)
    process_env = env or os.environ.copy()
    try:
        probe = subprocess.run(
            [executable, "-c", "import mcp"],
            capture_output=True,
            text=True,
            timeout=20,
            env=process_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not execute bundled Python: {exc}"
    if probe.returncode == 0:
        return True, "Python dependency mcp is already available."
    if offline:
        return False, "Python dependency mcp is missing; offline mode will not use pip."
    print("Installing Python dependency: mcp")
    try:
        result = subprocess.run(
            [executable, "-m", "pip", "install", "mcp"],
            env=process_env,
        )
    except OSError as exc:
        return False, f"Could not launch pip: {exc}"
    if result.returncode == 0:
        return True, "Installed mcp."
    return False, "Could not install mcp with pip."


def _load_bundle(args) -> engine_bundle.EngineBundle | None:
    if args.bundle:
        return engine_bundle.load_engine_bundle(args.bundle, verify_hashes=True)
    return engine_bundle.discover_engine_bundle(ROOT, verify_hashes=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print choices without installing models or starting runtimes",
    )
    parser.add_argument("--model", default="", help="override the base model")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never use pip or an Ollama model registry",
    )
    parser.add_argument(
        "--bundle",
        default="",
        help=f"explicit directory containing {engine_bundle.MANIFEST_NAME}",
    )
    args = parser.parse_args(argv)

    try:
        bundle = _load_bundle(args)
    except ValueError as exc:
        print(f"  bundle: INVALID - {exc}", file=sys.stderr)
        return 4

    ram = total_ram_gb()
    offline = args.offline or bundle is not None
    requested = args.model.strip() or os.environ.get("TRILOBITE_BASE_MODEL", "").strip()
    try:
        model = (
            engine_bundle.select_base_model(bundle, ram, requested)
            if bundle is not None
            else requested or choose_model(ram)
        )
    except ValueError as exc:
        print(f"  model: INVALID - {exc}", file=sys.stderr)
        return 4

    print("Trilobite engine bootstrap")
    print("  system: %s %s" % (platform.system(), platform.machine()))
    print("  detected RAM: %.1f GB" % ram if ram else "  detected RAM: unknown")
    print("  selected model: %s" % model)
    print("  network policy: %s" % ("offline" if offline else "online fallback allowed"))
    if bundle is not None:
        print("  bundle: %s (%s)" % (bundle.root, bundle.identity))
        print("  bundle integrity: verified")
    else:
        print("  bundle: none; using host runtimes")

    if args.dry_run:
        return 0

    process_env = os.environ.copy()
    python_executable = Path(sys.executable)
    ollama = _ollama_executable()
    if bundle is not None:
        try:
            model_store, copied, reused = engine_bundle.install_model_store(bundle)
        except (OSError, ValueError) as exc:
            print(f"  models: Could not install sealed model store: {exc}", file=sys.stderr)
            return 4
        process_env.update(engine_bundle.runtime_environment(bundle, model_store))
        python_executable = bundle.python_executable
        ollama = str(bundle.ollama_executable)
        print(
            "  models: installed %d sealed file(s), reused %d in %s"
            % (copied, reused, model_store)
        )

    ok, msg = ensure_python_deps(
        python_executable,
        offline=offline,
        env=process_env,
    )
    print("  python: %s" % msg)
    if not ok:
        return 3

    ok, msg = ensure_ollama_running(ollama, env=process_env)
    print("  ollama: %s" % msg)
    if not ok:
        return 2

    process_env["TRILOBITE_BASE_MODEL"] = model
    process_env.setdefault("LOCAL_LLM_NUM_THREAD", str(os.cpu_count() or 4))
    process_env.setdefault("LOCAL_LLM_NUM_GPU", "999")
    process_env.setdefault("LOCAL_LLM_NUM_BATCH", "512")
    process_env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    command = [
        str(python_executable),
        str(ROOT / "setup_alias.py"),
        "--model",
        model,
        "--embed-model",
        process_env.get("TRILOBITE_EMBED_MODEL", "nomic-embed-text"),
        "--ollama",
        ollama,
    ]
    if offline:
        command.append("--offline")
    result = _run(command, env=process_env, cwd=str(ROOT))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
