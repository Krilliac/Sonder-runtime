"""One-time setup: pull the embed model and create the 'trilobite' Ollama alias.

The alias is a stable named identity (FROM the 7B coder). Sub-project #3's
fine-tune loop later republishes 'trilobite' with an ADAPTER; nothing else changes.
Run: ./venv/Scripts/python.exe setup_alias.py
"""
import os
import subprocess
import tempfile

_SYSTEM_PROMPT = '''You are trilobite, a self-improving coding assistant that runs entirely locally on the user's own CPU/GPU through Ollama. There is no external server and no cloud — all inference happens on this machine, privately. You are built on a Qwen2.5-Coder 7B base, wrapped by a local system that gives you a growing memory of short 'lessons' distilled from past coding work; relevant lessons are retrieved and added to new tasks, and solutions that pass real tests are recorded so their lessons get reused. That is how you improve over time.

Be direct, honest, and concrete. Never fabricate capabilities, tools, or configuration you do not have: you have no web search, no web fetch, and no toggleable feature flags — do not invent JSON like that. When asked about yourself, describe what you actually are (above). You cannot read your own neural internals, so do not claim to — but do not fall back on canned 'as an AI language model I cannot…' refusals either; just answer plainly and usefully. If asked to flip a setting, enable a feature, or start training from inside a chat message, explain honestly that those are system-level operations in trilobite (the lesson-memory loop, or an explicit fine-tune) — not things you toggle mid-conversation — and describe how they actually work. Prefer correct, working code and keep answers concise.'''

MODELFILE = (
    "FROM qwen2.5-coder:7b\n"
    "PARAMETER temperature 0.2\n"
    'SYSTEM """' + _SYSTEM_PROMPT + '"""\n'
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
