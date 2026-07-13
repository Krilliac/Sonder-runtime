"""Create Sonder Runtime's stable Ollama alias safely online or offline."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile

import ollama_endpoint


DEFAULT_BASE_MODEL = "qwen2.5-coder:7b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
STABLE_ALIAS = "sonder:latest"

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
    parser.add_argument("--model", default=os.environ.get("SONDER_BASE_MODEL", DEFAULT_BASE_MODEL))
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
    args = parser.parse_args(argv)
    base_model = args.model.strip()
    embed_model = args.embed_model.strip()
    if not base_model or not embed_model:
        parser.error("model names may not be empty")

    try:
        env = ollama_endpoint.client_environment(os.environ)
    except ValueError as error:
        print("Ollama endpoint blocked: %s" % error)
        return 4

    ollama = ollama_executable(args.ollama)
    for model, label in ((base_model, "base"), (embed_model, "embedding")):
        ok, message = ensure_model(ollama, model, offline=args.offline, env=env)
        print(f"  {label}: {message}")
        if not ok:
            return 2
    ok, message = create_alias(ollama, base_model, env=env)
    print(f"  alias: {message}")
    if not ok:
        return 3
    print("Done. Verify with: ollama list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
