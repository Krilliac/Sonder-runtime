import json
import os
import pytest

from sonder_runtime.adapters.static_artifact_validation import StaticArtifactEvidence


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _assess(root, name, content, objective=""):
    path = _write(root, name, content)
    evidence = StaticArtifactEvidence(root, objective)
    args = {"path": str(path)}
    evidence.observe("file_write", args, success=True)
    evidence.observe("file_read", args, output=content, success=True)
    return evidence.assess()


def test_html_readback_and_static_check_passes_without_execution(tmp_path):
    result = _assess(
        tmp_path,
        "starfield.html",
        "<!doctype html><html><body><canvas></canvas><script>requestAnimationFrame(() => {});</script></body></html>",
        "make a self-contained HTML page",
    )
    assert result["passed"]
    assert result["evidence"][0]["status"] == "passed"
    assert result["evidence"][0]["readback_tool"] == "file_read"
    assert result["evidence"][0]["checker"] == "html_parser"


def test_html_unclosed_and_external_dependency_fail(tmp_path):
    unclosed = _assess(tmp_path, "bad.html", "<html><body><main>oops</body></html>")
    assert not unclosed["passed"]
    assert "unclosed" in unclosed["error"]

    external = _assess(
        tmp_path,
        "cdn.html",
        '<!doctype html><html><head><link href="https://cdn.example/site.css"></head></html>',
        "self-contained page",
    )
    assert not external["passed"]
    assert "external URL" in external["error"]


def test_python_json_and_toml_are_checked_without_execution(tmp_path):
    assert _assess(tmp_path, "script.py", "value = 1\n")["passed"]
    assert _assess(tmp_path, "data.json", json.dumps({"ok": True}))["passed"]
    assert _assess(tmp_path, "settings.toml", "enabled = true\n")["passed"]
    invalid = _assess(tmp_path, "broken.py", "if:\n")
    assert not invalid["passed"]


def test_missing_readback_and_later_mutation_invalidate_evidence(tmp_path):
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    args = {"path": str(path)}
    evidence.observe("file_write", args, success=True)
    assert evidence.assess()["evidence"][0]["status"] == "missing_readback"
    evidence.observe("file_read", args, success=True)
    assert evidence.assess()["passed"]
    path.write_text("<html><body></html>", encoding="utf-8")
    evidence.observe("file_edit", args, success=True)
    assert evidence.assess()["evidence"][0]["status"] == "missing_readback"


def test_failed_mutation_cannot_be_rescued_by_a_later_readback(tmp_path):
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    args = {"path": str(path)}
    evidence.observe("file_write", args, success=False)
    evidence.observe("file_read", args, success=True)
    result = evidence.assess()
    assert not result["passed"]
    assert any(item["status"] == "failed" for item in result["evidence"])


def test_replacement_after_readback_is_rejected(tmp_path):
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    args = {"path": str(path)}
    evidence.observe("file_write", args, success=True)
    evidence.observe("file_read", args, success=True)
    replacement = path.with_suffix(".replacement")
    replacement.write_text("<html></html>", encoding="utf-8")
    path.unlink()
    replacement.replace(path)
    result = evidence.assess()
    assert not result["passed"]
    assert "identity" in result["error"]


def test_symlink_paths_are_rejected_when_supported(tmp_path):
    target = _write(tmp_path, "target.html", "<html></html>")
    link = tmp_path / "link.html"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    evidence = StaticArtifactEvidence(tmp_path)
    evidence.observe("file_write", {"path": str(link)}, success=True)
    assert evidence.assess()["passed"] is False
    assert evidence.assess()["deferable"] is False


def test_paths_outside_project_are_not_accepted(tmp_path):
    outside = tmp_path.parent / "outside.html"
    outside.write_text("<html></html>", encoding="utf-8")
    evidence = StaticArtifactEvidence(tmp_path)
    args = {"path": str(outside)}
    evidence.observe("file_write", args, success=True)
    assert evidence.assess()["passed"] is False
    assert evidence.assess()["deferable"] is False


def test_unknown_extension_is_explicitly_unsupported(tmp_path):
    result = _assess(tmp_path, "artifact.bin", "opaque")
    assert result["unsupported"]
    assert not result["passed"]
    assert result["evidence"][0]["status"] == "unsupported"


def test_unknown_mutation_cannot_hide_behind_a_known_valid_file(tmp_path):
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    evidence.observe("file_write", {"path": str(path)}, success=True)
    evidence.observe("file_read", {"path": str(path)}, success=True)
    evidence.observe("opaque_mutator", {}, success=True, mutation=True)
    assert not evidence.assess()["passed"]
    assert not evidence.assess()["deferable"]


def test_outside_target_in_multi_file_mutation_is_not_dropped(tmp_path):
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    evidence.observe("file_batch_write", {"files": [
        {"path": str(path)}, {"path": str(tmp_path.parent / "outside.html")},
    ]}, success=True, mutation=True)
    evidence.observe("file_read", {"path": str(path)}, success=True)
    assert not evidence.assess()["passed"]


def test_binary_unknown_extension_can_be_deferred(tmp_path):
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"\xff\xfe\x00")
    evidence = StaticArtifactEvidence(tmp_path)
    evidence.observe("file_write", {"path": str(path)}, success=True)
    evidence.observe("file_read", {"path": str(path)}, success=True)
    assert evidence.assess()["deferable"]
    assert not evidence.assess()["passed"]


def test_change_during_static_check_is_rejected(tmp_path, monkeypatch):
    from sonder_runtime.adapters import static_artifact_validation as module
    path = _write(tmp_path, "page.html", "<html></html>")
    evidence = StaticArtifactEvidence(tmp_path)
    evidence.observe("file_write", {"path": str(path)}, success=True)
    evidence.observe("file_read", {"path": str(path)}, success=True)
    original = module._validate

    def validate(*args):
        result = original(*args)
        path.write_text("<html><body>", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "_validate", validate)
    result = evidence.assess()
    assert not result["passed"]
    assert "during static validation" in result["error"]


@pytest.mark.parametrize("name,content,passed", [
    ("x.json", "{", False), ("x.toml", "x = [", False),
    ("x.svg", '<svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>', True),
    ("x.svg", '<svg><image href="https://cdn.example/x.png"/></svg>', False),
    ("x.xml", "<root><x/></root>", True), ("x.xml", "<root>", False),
    ("x.htm", '<html><img src="//cdn.example/x.png"></html>', False),
    ("x.py", "raise RuntimeError('must not execute')\n", True),
])
def test_supported_checkers_and_self_containment(tmp_path, name, content, passed):
    result = _assess(tmp_path, name, content, "self-contained")
    assert result["passed"] is passed
    assert result["evidence"][0]["checker"]
