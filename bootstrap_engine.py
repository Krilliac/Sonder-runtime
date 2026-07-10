"""One-click local engine bootstrap for bundled Trilobite installs.

Starts Ollama if needed, detects available memory, chooses a conservative local
coder model, pulls the model + embeddings, and creates the stable `trilobite`
alias through setup_alias.py. Stdlib only.
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


MODEL_SMALL = "qwen2.5-coder:1.5b"
MODEL_MEDIUM = "qwen2.5-coder:3b"
MODEL_LARGE = "qwen2.5-coder:7b"
ROOT = Path(__file__).resolve().parent


def _run(cmd, check=False, env=None, cwd=None):
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, env=env, cwd=cwd)


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
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                text=True,
                timeout=10,
            ).strip()
            return int(out) / (1024 ** 3)
        except Exception:
            return 0.0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return int(out) / (1024 ** 3)
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


def ensure_ollama_running():
    if not shutil.which("ollama"):
        return False, "Ollama is not installed or not on PATH."
    probe = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if probe.returncode == 0:
        return True, "Ollama is already running."
    print("Starting Ollama...")
    if os.name == "nt":
        subprocess.Popen(["ollama", "serve"], creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        probe = subprocess.run(["ollama", "list"], capture_output=True, text=True)
        if probe.returncode == 0:
            return True, "Ollama started."
    return False, "Ollama did not become reachable after startup."


def ensure_python_deps():
    try:
        import mcp  # noqa: F401
        return True, "Python dependency mcp is already available."
    except Exception:
        pass
    print("Installing Python dependency: mcp")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "mcp"])
    if result.returncode == 0:
        return True, "Installed mcp."
    return False, "Could not install mcp with pip."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print detected choices without pulling models")
    parser.add_argument("--model", default="", help="override base model, e.g. qwen2.5-coder:3b")
    args = parser.parse_args(argv)

    ram = total_ram_gb()
    model = args.model.strip() or choose_model(ram)
    print("Trilobite engine bootstrap")
    print("  system: %s %s" % (platform.system(), platform.machine()))
    print("  detected RAM: %.1f GB" % ram if ram else "  detected RAM: unknown")
    print("  selected model: %s" % model)

    if args.dry_run:
        return 0

    ok, msg = ensure_python_deps()
    print("  python: %s" % msg)
    if not ok:
        return 3

    ok, msg = ensure_ollama_running()
    print("  ollama: %s" % msg)
    if not ok:
        return 2

    env = os.environ.copy()
    env["TRILOBITE_BASE_MODEL"] = model
    env.setdefault("LOCAL_LLM_NUM_THREAD", str(os.cpu_count() or 4))
    env.setdefault("LOCAL_LLM_NUM_GPU", "999")
    env.setdefault("LOCAL_LLM_NUM_BATCH", "512")
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    result = _run(
        [sys.executable, str(ROOT / "setup_alias.py")],
        env=env,
        cwd=str(ROOT),
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
