"""Create the local-system payload shipped beside Flutter desktop builds.

The payload contains the runnable trilobite system code, launch scripts, tests,
and docs. It intentionally excludes local/private/generated data such as memory.db,
venvs, model checkpoints, caches, and build output.
"""
from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "venv",
    "trilobite-lora",
    "trilobite-personal-lora",
    "app/build",
    "app/.dart_tool",
    "app/assets",
    "app/android",
    "app/linux",
    "app/windows",
    "app/macos",
    "app/ios",
}

EXCLUDE_FILES = {
    "memory.db",
    "memory.db-shm",
    "memory.db-wal",
    "combined_personal.jsonl",
    "combined_training.jsonl",
    "personal_dataset.jsonl",
    "training_data.jsonl",
}


def _excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if path.name in EXCLUDE_FILES:
        return True
    parts = set(rel.split("/"))
    if parts & {".git", ".pytest_cache", "__pycache__", "venv"}:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS)


def copy_payload(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in ROOT.rglob("*"):
        if _excluded(path):
            continue
        rel = path.relative_to(ROOT)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (dest / "BUNDLED_SYSTEM_README.txt").write_text(
        "This folder is the bundled trilobite local system.\n"
        "Run trilobite-serve.cmd on Windows, or python trilobite_serve.py on Linux/macOS.\n"
        "Run endless-train.cmd for continuous grounded local training.\n"
        "Model weights are managed by Ollama and may be pulled on first setup.\n",
        encoding="utf-8",
    )


def zip_payload(src: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src.parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="app/build/local-system")
    parser.add_argument("--zip", default="")
    args = parser.parse_args()

    out = (ROOT / args.out).resolve()
    if ROOT not in out.parents and out != ROOT:
        raise SystemExit("--out must stay inside the repository")
    copy_payload(out)
    if args.zip:
        zip_payload(out, (ROOT / args.zip).resolve())
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
