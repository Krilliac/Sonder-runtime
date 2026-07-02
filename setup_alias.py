"""One-time setup: pull the embed model and create the 'trilobite' Ollama alias.

The alias is a stable named identity (FROM the 7B coder). Sub-project #3's
fine-tune loop later republishes 'trilobite' with an ADAPTER; nothing else changes.
Run: ./venv/Scripts/python.exe setup_alias.py
"""
import os
import subprocess
import tempfile

MODELFILE = (
    "FROM qwen2.5-coder:7b\n"
    "PARAMETER temperature 0.2\n"
    'SYSTEM "You are trilobite, a local self-improving coding assistant. '
    'Be concise and correct; prefer working code."\n'
)


def main():
    print("Pulling embed model nomic-embed-text ...")
    subprocess.run(["ollama", "pull", "nomic-embed-text"], check=False)
    fd, path = tempfile.mkstemp(suffix=".Modelfile")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(MODELFILE)
        print("Creating 'trilobite' alias ...")
        subprocess.run(["ollama", "create", "trilobite", "-f", path], check=False)
    finally:
        os.unlink(path)
    print("Done. Verify with: ollama list | findstr trilobite")


if __name__ == "__main__":
    main()
