"""scripts/build_web_app.py: manifest, publish/prune and the refusal guards.

Flutter is replaced by a stub that writes a tiny web build, so no SDK is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.interfaces.http import web_app

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_web_app.py"
_spec = importlib.util.spec_from_file_location("build_web_app_under_test", SCRIPT)
bwa = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bwa
_spec.loader.exec_module(bwa)

STUB = '''\
import os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_FLUTTER_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
if args[:1] == ["--version"]:
    print("Flutter 9.9.9 stub")
elif args[:2] == ["build", "web"]:
    out = Path(args[args.index("-o") + 1])
    (out / "canvaskit").mkdir(parents=True)
    index = os.environ.get("FAKE_INDEX", '<html><script src="flutter_bootstrap.js" async></script></html>')
    (out / "index.html").write_text(index, encoding="utf-8")
    (out / "flutter_bootstrap.js").write_text(os.environ.get("FAKE_BOOT", '{"useLocalCanvasKit":true}'), encoding="utf-8")
    (out / "main.dart.js").write_text("// app", encoding="utf-8")
    (out / ".last_build_id").write_text("x", encoding="utf-8")
    (out / "canvaskit" / ".gitkeep").write_text("", encoding="utf-8")
    (out / "canvaskit" / "canvaskit.wasm").write_bytes(b"\\x00asm\\x01\\x00\\x00\\x00")
'''


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.test",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture()
def fake_flutter(tmp_path):
    stub = tmp_path / "stub_flutter.py"
    stub.write_text(STUB, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "flutter.cmd"
        launcher.write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "flutter"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """A main checkout plus a linked worktree, both with app/pubspec.yaml."""
    monkeypatch.setenv("FAKE_FLUTTER_LOG", str(tmp_path / "flutter.log"))
    main = tmp_path / "main"
    (main / "app").mkdir(parents=True)
    (main / "app" / "pubspec.yaml").write_text("name: x\n", encoding="utf-8")
    _git(main, "init", "-q", "-b", "trunk")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    return main, linked


def _run(app_dir, out, flutter, *extra):
    return bwa.main(["--app-dir", str(app_dir), "--web-app-dir", str(out), "--flutter",
                     str(flutter), *extra])


def test_publishes_a_verifiable_release(tmp_path, repos, fake_flutter):
    _main, linked = repos
    out = tmp_path / "out"
    assert _run(linked / "app", out, fake_flutter) == 0
    pointer = json.loads((out / "current.json").read_text(encoding="utf-8"))
    release = out / "releases" / pointer["release"]
    manifest = json.loads((release / "sonder-web-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1 and manifest["dirty"] is False
    assert len(manifest["commit"]) == 40 and manifest["flutter"] == "Flutter 9.9.9 stub"
    assert {"index.html", "main.dart.js", "flutter_bootstrap.js",
            "canvaskit/canvaskit.wasm"} <= set(manifest["files"])
    assert "sonder-web-manifest.json" not in manifest["files"]
    assert not any(part.startswith(".") for rel in manifest["files"] for part in rel.split("/"))
    assert not (release / ".last_build_id").exists()
    assert not list(out.glob("staging-*"))
    web_app.clear_cache()
    assert web_app.load_bundle(out).ok  # the server accepts what the script wrote
    log = (tmp_path / "flutter.log").read_text(encoding="utf-8")
    for flag in ("--no-web-resources-cdn", "--csp", "--base-href /app/", "-O 4", "--no-source-maps"):
        assert flag in log


def test_main_checkout_is_refused_unless_allowed(tmp_path, repos, fake_flutter, capsys):
    main, _linked = repos
    out = tmp_path / "out"
    assert _run(main / "app", out, fake_flutter) == 1
    assert "main checkout" in capsys.readouterr().err
    assert not (out / "current.json").exists()
    assert _run(main / "app", out, fake_flutter, "--allow-main-checkout") == 0


def test_output_inside_the_repo_is_refused(repos, fake_flutter, capsys):
    _main, linked = repos
    assert _run(linked / "app", linked / "inside", fake_flutter) == 1
    assert "inside the source checkout" in capsys.readouterr().err


def test_dirty_tree_is_refused_unless_allowed_and_recorded(tmp_path, repos, fake_flutter, capsys):
    _main, linked = repos
    (linked / "app" / "pubspec.yaml").write_text("name: changed\n", encoding="utf-8")
    out = tmp_path / "out"
    assert _run(linked / "app", out, fake_flutter) == 1
    assert "uncommitted" in capsys.readouterr().err
    assert _run(linked / "app", out, fake_flutter, "--allow-dirty") == 0
    release = next((out / "releases").iterdir())
    assert json.loads((release / "sonder-web-manifest.json").read_text(encoding="utf-8"))["dirty"] is True


@pytest.mark.parametrize("env_name,value,message", [
    ("FAKE_INDEX", "<html><script>alert(1)</script></html>", "inline"),
    ("FAKE_INDEX", '<script src="https://unpkg.com/x.js"></script>', "unpkg"),
    ("FAKE_BOOT", '{"useLocalCanvasKit":false}', "useLocalCanvasKit"),
])
def test_bad_output_is_refused_and_leaves_nothing(tmp_path, repos, fake_flutter, monkeypatch,
                                                  capsys, env_name, value, message):
    _main, linked = repos
    monkeypatch.setenv(env_name, value)
    out = tmp_path / "out"
    assert _run(linked / "app", out, fake_flutter) == 1
    assert message in capsys.readouterr().err
    assert not (out / "current.json").exists()
    assert not list(out.glob("staging-*"))


def test_failed_flutter_publishes_nothing(tmp_path, repos, capsys):
    _main, linked = repos
    out = tmp_path / "out"
    assert bwa.main(["--app-dir", str(linked / "app"), "--web-app-dir", str(out),
                     "--flutter", str(tmp_path / "missing-flutter")]) == 1
    assert not (out / "current.json").exists()


def test_prune_keeps_newest_and_never_the_current(tmp_path):
    out = tmp_path
    names = ["a-20260101000000", "b-20260102000000", "c-20260103000000", "d-20260104000000"]
    for name in names:
        (out / "releases" / name).mkdir(parents=True)
    (out / "current.json").write_text(json.dumps({"release": names[0]}), encoding="utf-8")
    removed = bwa.prune(out, 2)
    assert sorted(removed) == [names[1]]
    assert sorted(p.name for p in (out / "releases").iterdir()) == [names[0], names[2], names[3]]


def test_publish_swaps_current_atomically_and_refuses_an_existing_release(tmp_path):
    out = tmp_path / "out"
    for name in ("one", "two"):
        staging = out / f"staging-{name}"
        staging.mkdir(parents=True)
        bwa.publish(out, staging, name)
        assert json.loads((out / "current.json").read_text(encoding="utf-8")) == {"release": name}
    assert not (out / "current.json.tmp").exists()
    again = out / "staging-again"
    again.mkdir()
    with pytest.raises(bwa.BuildError):
        bwa.publish(out, again, "two")


def test_manifest_writer_hashes_every_file_and_skips_itself(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.js").write_bytes(b"abc")
    (tmp_path / "index.html").write_bytes(b"")
    manifest = bwa.write_manifest(tmp_path, commit="c" * 40, dirty=False, flutter_version="v")
    assert manifest["files"]["a/x.js"] == {
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "size": 3}
    assert "sonder-web-manifest.json" not in manifest["files"]
    assert (tmp_path / "sonder-web-manifest.json").is_file()
