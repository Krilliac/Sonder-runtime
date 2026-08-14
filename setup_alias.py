"""Create Sonder Runtime's stable Ollama alias safely online or offline."""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import tempfile

import bootstrap_engine
import ollama_endpoint


DEFAULT_EMBED_MODEL = "nomic-embed-text"
STABLE_ALIAS = "sonder:latest"


def default_base_model() -> str:
    """Choose a practical model from live RAM unless the operator pins one."""
    return os.environ.get("SONDER_BASE_MODEL", "").strip() or bootstrap_engine.choose_model()

# Directories a mounted removable drive (e.g. an "Open Source Everything"
# facts. USB) typically appears under, per platform. Used only when the
# operator asks to import a GGUF from USB; nothing is scanned automatically.
_USB_MOUNT_GLOBS = (
    "/media/*/*",      # Linux (udisks)
    "/run/media/*/*",  # Linux (systemd)
    "/mnt/*",          # Linux (manual mounts)
    "/Volumes/*",      # macOS
)

_SYSTEM_PROMPT = '''You are the local language model operating inside Sonder Runtime. Sonder is the host orchestration runtime, not a foundation model or a set of weights. The runtime gives you private memory, guarded file and program tools, artifact generation, orchestration, and optional web or hosted-model tools when those capabilities are explicitly exposed for the current request. Use tools that the host lists; never deny a listed capability merely because a base language model would not normally have it. Never invent tools, permissions, results, location, or configuration that the host did not provide.

Relevant lessons from grounded past work may be retrieved into new tasks, and outcomes that compile, pass tests, or are accepted can become reusable lessons. Be direct, honest, and concrete. Do not expose hidden chain-of-thought. Report observable actions, evidence, failures, and remaining work. Prefer correct working code, make progress autonomously within granted permissions, and keep answers concise unless detail is useful.'''


def model_file(base_model: str) -> str:
    return (
        f"FROM {base_model}\n"
        "PARAMETER temperature 0.2\n"
        f'SYSTEM """{_SYSTEM_PROMPT}"""\n'
    )


def ollama_executable(explicit: str = "") -> str:
    candidate = explicit.strip() or os.environ.get("SONDER_OLLAMA_EXE", "").strip()
    if candidate:
        return candidate
    return shutil.which("ollama") or "ollama"


def _run(ollama: str, args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess:
    print("+ " + " ".join([ollama, *args]))
    client_env = ollama_endpoint.client_environment(env)
    return subprocess.run(
        [ollama, *args],
        env=client_env,
        text=True,
        capture_output=True,
    )


def find_gguf_files(roots) -> list[str]:
    """Return .gguf paths found (non-recursively deep) under given roots.

    Each root is scanned itself and one directory level down, which covers a
    USB stick that keeps models at its top level or in a ``models/`` folder
    without walking an entire filesystem.
    """
    found: list[str] = []
    for root in roots:
        root = os.path.expanduser(str(root))
        if os.path.isfile(root) and root.lower().endswith(".gguf"):
            found.append(root)
            continue
        if not os.path.isdir(root):
            continue
        patterns = (
            os.path.join(root, "*.gguf"),
            os.path.join(root, "*", "*.gguf"),
        )
        for pattern in patterns:
            found.extend(sorted(glob.glob(pattern)))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for path in found:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            unique.append(path)
    return unique


def discover_usb_gguf(extra_roots=()) -> list[str]:
    """Enumerate GGUF files on mounted removable media (and any extra roots)."""
    roots: list[str] = []
    for pattern in _USB_MOUNT_GLOBS:
        roots.extend(sorted(glob.glob(pattern)))
    roots.extend(str(r) for r in extra_roots)
    return find_gguf_files(roots)


def import_gguf(
    ollama: str,
    gguf_path: str,
    model_name: str,
    *,
    env: dict[str, str],
) -> tuple[bool, str]:
    """Register a local .gguf file with Ollama under ``model_name``.

    Uses an Ollama Modelfile ``FROM <absolute .gguf path>``; Ollama copies
    the weights into its own store, so the USB can be removed afterward.
    This is the offline import path for a portable model such as the
    facts. USB (Qwen 3.x 4B) — no registry contact.
    """
    gguf_path = os.path.abspath(os.path.expanduser(gguf_path))
    if not os.path.isfile(gguf_path):
        return False, f"GGUF file not found: {gguf_path}"
    if not gguf_path.lower().endswith(".gguf"):
        return False, f"not a .gguf file: {gguf_path}"
    fd, path = tempfile.mkstemp(suffix=".Modelfile")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("FROM %s\n" % gguf_path)
        created = _run(ollama, ["create", model_name, "-f", path], env=env)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if created.returncode == 0:
        return True, f"Imported {os.path.basename(gguf_path)} as {model_name}."
    detail = (created.stderr or created.stdout).strip()
    return False, f"Could not import {gguf_path}: {detail or 'ollama create failed'}"


def ensure_model(
    ollama: str,
    model: str,
    *,
    offline: bool,
    env: dict[str, str],
) -> tuple[bool, str]:
    present = _run(ollama, ["show", model], env=env)
    if present.returncode == 0:
        return True, f"{model} is already available."
    if offline:
        return False, f"{model} is missing; offline mode will not contact a registry."
    pulled = _run(ollama, ["pull", model], env=env)
    if pulled.returncode == 0:
        return True, f"Downloaded {model}."
    detail = (pulled.stderr or pulled.stdout).strip()
    return False, f"Could not download {model}: {detail or 'ollama pull failed'}"


def create_alias(
    ollama: str,
    base_model: str,
    *,
    env: dict[str, str],
) -> tuple[bool, str]:
    fd, path = tempfile.mkstemp(suffix=".Modelfile")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(model_file(base_model))
        created = _run(ollama, ["create", STABLE_ALIAS, "-f", path], env=env)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if created.returncode == 0:
        return True, f"Created the {STABLE_ALIAS} alias."
    detail = (created.stderr or created.stdout).strip()
    return False, f"Could not create {STABLE_ALIAS}: {detail or 'ollama create failed'}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get("SONDER_BASE_MODEL", "").strip(),
        help="Base Ollama model (default: choose from live host memory)",
    )
    parser.add_argument(
        "--embed-model",
        default=os.environ.get("SONDER_EMBED_MODEL", DEFAULT_EMBED_MODEL),
    )
    parser.add_argument("--ollama", default="")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use only models already available to the local Ollama server",
    )
    parser.add_argument(
        "--gguf",
        default="",
        help="import a local .gguf file as the base model and alias it "
             "(e.g. a facts. USB model); implies offline",
    )
    parser.add_argument(
        "--from-usb",
        action="store_true",
        help="discover a .gguf on mounted removable media and import it; "
             "prompts if several are found unless --gguf pins one",
    )
    parser.add_argument(
        "--usb-root",
        action="append",
        default=[],
        help="additional mount path to scan for a .gguf (repeatable)",
    )
    args = parser.parse_args(argv)
    embed_model = args.embed_model.strip()

    try:
        env = ollama_endpoint.client_environment(os.environ)
    except ValueError as error:
        print("Ollama endpoint blocked: %s" % error)
        return 4
    ollama = ollama_executable(args.ollama)

    # Portable-model path: import a .gguf from a file or USB and alias it.
    gguf_path = args.gguf.strip()
    if args.from_usb and not gguf_path:
        candidates = discover_usb_gguf(args.usb_root)
        if not candidates:
            print("  usb: no .gguf found on mounted removable media"
                  + (" or %s" % ", ".join(args.usb_root) if args.usb_root else ""))
            return 2
        if len(candidates) > 1:
            print("  usb: multiple GGUF files found; re-run with --gguf <path>:")
            for path in candidates:
                print("    - %s" % path)
            return 2
        gguf_path = candidates[0]
    if gguf_path:
        ok, message = import_gguf(ollama, gguf_path, STABLE_ALIAS, env=env)
        print(f"  import: {message}")
        if not ok:
            return 2
        ok, message = ensure_model(
            ollama, embed_model, offline=True, env=env,
        )
        print(f"  embedding: {message}")
        if not ok:
            print("  note: recall/lessons need an embedding model; pull "
                  "nomic-embed-text when back online, or set SONDER_EMBED_MODEL.")
        print("Done. %s now serves the imported model. Verify: ollama list"
              % STABLE_ALIAS)
        return 0

    base_model = args.model.strip() or default_base_model()
    if not base_model or not embed_model:
        parser.error("model names may not be empty")

    ok, message = ensure_model(ollama, base_model, offline=args.offline, env=env)
    print(f"  base: {message}")
    if not ok:
        return 2

    # Core REPL/API chat needs the generative base model only.  Try to make a
    # fresh setup feature-complete by acquiring the default embedder too, but
    # do not discard a usable local chat installation when an optional pull is
    # offline, unavailable, or rejected by the provider.
    ok, message = ensure_model(ollama, embed_model, offline=args.offline, env=env)
    print(f"  embedding: {message}")
    if not ok:
        print("  note: core chat is ready; recall/lessons need an embedding model. "
              "Pull %s later, or set SONDER_EMBED_MODEL." % embed_model)
    ok, message = create_alias(ollama, base_model, env=env)
    print(f"  alias: {message}")
    if not ok:
        return 3
    print("Done. Verify with: ollama list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
