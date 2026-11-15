"""Build the Flutter web app and publish it for the Sonder runtime to serve.

The runtime serves ``<web-app-dir>/current.json`` -> ``releases/<name>`` at
``/app/`` when ``[server].web_app_enabled`` is on.  This script produces that
layout::

    python scripts/build_web_app.py            # build + publish + prune
    python scripts/build_web_app.py --web-app-dir D:\\sonder-web --keep 5

Each release holds the build output plus ``sonder-web-manifest.json`` (sha256
and size of every file).  Publishing is an atomic ``os.replace`` of
``current.json``, so a running server picks the new release up without a
restart and no symlink or junction is needed on Windows.

Guards (each can be overridden by an explicit flag):
  * the web-app-dir must not be inside the source checkout;
  * the source must be a linked worktree, not the main checkout;
  * the source tree must be clean (a dirty build is recorded as dirty=true).

Exit code 0 on success, 1 on a refused guard or a failed build.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "sonder-web-manifest.json"
CURRENT_NAME = "current.json"
RELEASES_DIR = "releases"
BASE_HREF = "/app/"
_CHUNK = 1 << 20
_CDN_MARKERS = ("gstatic.com", "unpkg")
_LOCAL_CANVASKIT = re.compile(r'"useLocalCanvasKit"\s*:\s*true')
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE)


class BuildError(Exception):
    """A refused guard or a failed build step; the message is the remedy."""


def _run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
    try:
        done = subprocess.run(
            command, cwd=str(cwd), check=False, text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None)
    except OSError as exc:
        raise BuildError(f"cannot run {command[0]!r}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stdout or "").strip() if capture else ""
        raise BuildError(f"{' '.join(command[:3])} failed with exit code {done.returncode}"
                         + (f": {detail}" if detail else ""))
    return done.stdout or ""


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], repo, capture=True).strip()


def resolve_flutter(explicit: str | None, repo: Path, env: dict[str, str]) -> str:
    """``--flutter``, then ``$FLUTTER``, then the repo toolchain, then PATH."""
    if explicit:
        return explicit
    if env.get("FLUTTER"):
        return env["FLUTTER"]
    name = "flutter.bat" if os.name == "nt" else "flutter"
    local = repo / ".tooling" / "flutter" / "bin" / name
    if local.is_file():
        return str(local)
    found = shutil.which("flutter")
    if found:
        return found
    raise BuildError("flutter not found: pass --flutter, set FLUTTER, or put it on PATH")


def check_out_dir(web_app_dir: Path, repo: Path) -> Path:
    resolved = web_app_dir.resolve()
    root = repo.resolve()
    if resolved == root or root in resolved.parents:
        raise BuildError(
            f"refusing to publish inside the source checkout ({resolved}); "
            "pass --web-app-dir outside the repository")
    return resolved


def check_repo(repo: Path, *, allow_main: bool, allow_dirty: bool) -> tuple[str, bool]:
    """Return ``(commit, dirty)`` or raise when a guard refuses."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(_git(repo, "rev-parse", "--path-format=absolute",
                       "--git-common-dir")).resolve()
    if git_dir == common and not allow_main:
        raise BuildError(
            "the source is a main checkout; build from a linked worktree "
            "(git worktree add) or pass --allow-main-checkout")
    commit = _git(repo, "rev-parse", "HEAD")
    dirty = bool(_git(repo, "status", "--porcelain"))
    if dirty and not allow_dirty:
        raise BuildError("the working tree has uncommitted changes; commit them or pass --allow-dirty")
    return commit, dirty


def inspect_output(out: Path) -> None:
    """Fail on an inline script or a CDN reference that the CSP would block."""
    index = out / "index.html"
    if not index.is_file():
        raise BuildError("the build produced no index.html")
    if _INLINE_SCRIPT.search(index.read_text(encoding="utf-8", errors="replace")):
        raise BuildError("index.html has an inline <script>; the CSP forbids it")
    html = index.read_text(encoding="utf-8", errors="replace")
    for marker in _CDN_MARKERS:
        if marker in html:
            raise BuildError(f"index.html references {marker}; the CSP only allows this origin")
    bootstrap = out / "flutter_bootstrap.js"
    if not bootstrap.is_file():
        raise BuildError("the build produced no flutter_bootstrap.js")
    text = bootstrap.read_text(encoding="utf-8", errors="replace")
    # The loader keeps a gstatic CanvasKit URL as dead code; what matters is
    # that this build is configured to load its own CanvasKit.
    if _LOCAL_CANVASKIT.search(text) is None:
        raise BuildError("flutter_bootstrap.js does not set useLocalCanvasKit; "
                         "build with --no-web-resources-cdn")
    if "unpkg" in text:
        raise BuildError("flutter_bootstrap.js references unpkg")


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def strip_hidden(out: Path) -> list[str]:
    """Remove dotfiles (``.last_build_id``, ``.gitkeep``): the server never serves them."""
    removed: list[str] = []
    for directory, dirnames, names in os.walk(out, topdown=True):
        for name in list(dirnames):
            if name.startswith("."):
                dirnames.remove(name)
                shutil.rmtree(Path(directory) / name)
                removed.append(name)
        for name in names:
            if name.startswith("."):
                (Path(directory) / name).unlink()
                removed.append(name)
    return removed


def write_manifest(out: Path, *, commit: str, dirty: bool, flutter_version: str) -> dict:
    files: dict[str, dict[str, object]] = {}
    for directory, dirnames, names in os.walk(out):
        dirnames.sort()
        for name in sorted(names):
            path = Path(directory) / name
            rel = path.relative_to(out).as_posix()
            if rel == MANIFEST_NAME:
                continue
            if path.is_symlink():
                raise BuildError(f"the build output contains a link: {rel}")
            digest, size = hash_file(path)
            files[rel] = {"sha256": digest, "size": size}
    manifest = {
        "schema": 1, "commit": commit, "dirty": dirty, "flutter": flutter_version,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def publish(web_app_dir: Path, staging: Path, release_name: str) -> Path:
    """Move ``staging`` to ``releases/<name>`` and atomically repoint ``current.json``."""
    releases = web_app_dir / RELEASES_DIR
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / release_name
    if target.exists():
        raise BuildError(f"release already exists: {target}")
    os.replace(staging, target)
    pointer = web_app_dir / (CURRENT_NAME + ".tmp")
    pointer.write_text(json.dumps({"release": release_name}) + "\n", encoding="utf-8")
    os.replace(pointer, web_app_dir / CURRENT_NAME)
    return target


def prune(web_app_dir: Path, keep: int) -> list[str]:
    """Delete the oldest releases beyond ``keep``; the current one is never removed."""
    releases = web_app_dir / RELEASES_DIR
    if not releases.is_dir():
        return []
    try:
        current = json.loads((web_app_dir / CURRENT_NAME).read_text(encoding="utf-8")).get("release")
    except (OSError, ValueError):
        current = None
    entries = sorted((p for p in releases.iterdir() if p.is_dir() and not p.is_symlink()),
                     key=lambda p: (p.name.rsplit("-", 1)[-1], p.name), reverse=True)
    removed: list[str] = []
    for index, entry in enumerate(entries):
        if index < keep or entry.name == current:
            continue
        shutil.rmtree(entry)
        removed.append(entry.name)
    return removed


def _default_web_app_dir() -> tuple[Path, int]:
    sys.path.insert(0, str(REPO_ROOT))
    from sonder_runtime.platform.config import load_config, web_app_root

    config = load_config(None)
    return web_app_root(config), config.server.port


def build(args: argparse.Namespace, env: dict[str, str]) -> Path:
    app_dir = Path(args.app_dir).resolve()
    if not (app_dir / "pubspec.yaml").is_file():
        raise BuildError(f"{app_dir} is not a Flutter app (no pubspec.yaml)")
    repo = app_dir.parent
    port = 11435
    if args.web_app_dir:
        web_app_dir = Path(args.web_app_dir)
    else:
        web_app_dir, port = _default_web_app_dir()
    web_app_dir = check_out_dir(web_app_dir, repo)
    commit, dirty = check_repo(repo, allow_main=args.allow_main_checkout, allow_dirty=args.allow_dirty)
    flutter = resolve_flutter(args.flutter, repo, env)
    version = (_run([flutter, "--version"], app_dir, capture=True).strip().splitlines() or [""])[0]

    web_app_dir.mkdir(parents=True, exist_ok=True)
    staging = web_app_dir / f"staging-{uuid.uuid4().hex[:12]}"
    try:
        _run([flutter, "pub", "get"], app_dir)
        _run([flutter, "build", "web", "--release", "--no-web-resources-cdn", "--csp",
              "--base-href", BASE_HREF, "-O", "4", "--no-source-maps", "-o", str(staging)], app_dir)
        inspect_output(staging)
        strip_hidden(staging)
        write_manifest(staging, commit=commit, dirty=dirty, flutter_version=version)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        release = publish(web_app_dir, staging, f"{commit[:9]}-{stamp}")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    removed = prune(web_app_dir, max(1, args.keep))
    print(f"published {release}")
    if removed:
        print("pruned " + ", ".join(removed))
    print(f"serve with SONDER_WEB_APP=1 (and SONDER_WEB_APP_DIR={web_app_dir} if not the default); "
          f"then open http://127.0.0.1:{port}/app/")
    return release


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--flutter", help="flutter executable (default: $FLUTTER, .tooling, PATH)")
    parser.add_argument("--app-dir", default=str(REPO_ROOT / "app"))
    parser.add_argument("--web-app-dir", help="publish root (default: [server].web_app_dir or <state home>/web_app)")
    parser.add_argument("--keep", type=int, default=3, help="releases to keep (default 3)")
    parser.add_argument("--allow-main-checkout", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        build(args, dict(os.environ if env is None else env))
    except BuildError as exc:
        print(f"build_web_app: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
