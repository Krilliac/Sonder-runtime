"""Extended code_runner language coverage and fence routing."""
from __future__ import annotations

import shutil

import pytest

import code_runner
import grounding


NEW_LANGS = {
    "bash": ("bash", 'echo "sum=$((20 + 22))"', "sum=42"),
    "ruby": ("ruby", 'puts "rb#{20 + 22}"', "rb42"),
    "perl": ("perl", 'print "pl", 20 + 22, "\\n";', "pl42"),
    "php": ("php", '<?php echo "php", 20 + 22, "\\n";', "php42"),
    "lua": ("lua", 'print("lua" .. (20 + 22))', "lua42"),
    "go": ("go", 'package main\nimport "fmt"\nfunc main(){fmt.Println("go", 20+22)}', "go 42"),
    "java": ("java", 'public class snippet{public static void main(String[] a){System.out.println("java"+(20+22));}}', "java42"),
    "rust": ("rust", 'fn main(){println!("rust{}", 20 + 22);}', "rust42"),
    "typescript": ("typescript", 'const n: number = 20 + 22; console.log("ts" + n);', "ts42"),
}

_PROBE = {
    "bash": "bash", "ruby": "ruby", "perl": "perl", "php": "php",
    "lua": "lua", "go": "go", "java": "java", "rust": "rustc",
    "typescript": "node",
}


def test_all_new_languages_registered():
    for lang in NEW_LANGS:
        assert lang in code_runner.SUPPORTED_LANGUAGES


@pytest.mark.parametrize("lang", list(NEW_LANGS))
def test_language_runs_or_reports_missing(lang):
    canonical, code, expected = NEW_LANGS[lang]
    if shutil.which(_PROBE[lang]) is None:
        pytest.skip(f"{_PROBE[lang]} not installed in this environment")
    result = code_runner.run_code(code, language=canonical, timeout=60)
    assert result["ok"], result.get("stderr") or result.get("error")
    assert expected in (result["stdout"] or "").replace(" ", "") or \
        expected in (result["stdout"] or "")


@pytest.mark.parametrize("alias,canonical", [
    ("sh", "bash"), ("zsh", "bash"), ("rb", "ruby"), ("pl", "perl"),
    ("golang", "go"), ("ts", "typescript"), ("rs", "rust"),
])
def test_aliases_normalize(alias, canonical):
    assert code_runner.normalize_language(alias) == canonical


@pytest.mark.parametrize("fence,expected", [
    ("bash", "bash"), ("sh", "bash"), ("ruby", "ruby"), ("go", "go"),
    ("rust", "rust"), ("java", "java"), ("typescript", "typescript"),
    ("php", "php"), ("perl", "perl"), ("lua", "lua"), ("r", "r"),
])
def test_fence_routing(fence, expected):
    block = grounding.extract_runnable_code_block(
        "```%s\nprint(1)\n```" % fence
    )
    assert block is not None
    assert block["language"] == expected


def test_missing_runtime_returns_actionable_error(monkeypatch):
    # Force a missing interpreter and confirm a clean runner-level error.
    monkeypatch.setattr(code_runner.shutil, "which", lambda name: None)
    result = code_runner.run_code('print("x")', language="lua", timeout=5)
    assert not result["ok"]
    assert result.get("error") or result.get("stderr")


def test_unsupported_language_still_rejected():
    with pytest.raises(ValueError):
        code_runner.run_code("noop", language="brainfuck")
