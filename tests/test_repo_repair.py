"""Repo-repair campaign templates: planted bugs fail, canonical fixes pass."""
from pathlib import Path

import pytest

import server

_CANONICAL_FIXES = {
    "offbyone": (
        "def total(values):\n"
        "    result = 0\n"
        "    for value in values:\n"
        "        result += value\n"
        "    return result\n"
    ),
    "boundary": (
        "def bulk_discount(quantity):\n"
        "    return 0.1 if quantity >= 10 else 0.0\n"
    ),
    "mutabledefault": (
        "def add_tag(tag, tags=None):\n"
        "    if tags is None:\n"
        "        tags = []\n"
        "    tags.append(tag)\n"
        "    return tags\n"
    ),
    "missingkey": (
        "def word_counts(words):\n"
        "    counts = {}\n"
        "    for word in words:\n"
        "        counts[word] = counts.get(word, 0) + 1\n"
        "    return counts\n"
    ),
    "numericsort": (
        "def sort_ids(ids):\n"
        "    return sorted(ids, key=int)\n"
    ),
}


def _write_project(tmp_path, module_src, test_src):
    (tmp_path / "module.py").write_text(module_src, encoding="utf-8")
    (tmp_path / "test_module.py").write_text(test_src, encoding="utf-8")


@pytest.mark.parametrize(
    "name,module_src,test_src", server._REPO_REPAIR_TASKS,
)
def test_planted_bug_fails_and_canonical_fix_passes(
    tmp_path, name, module_src, test_src,
):
    project = Path(tmp_path) / name
    project.mkdir()
    _write_project(project, module_src, test_src)
    ok, output = server._repo_repair_pytest(project, timeout=60)
    assert not ok, "template %s must fail before repair: %s" % (name, output)

    fixed = _CANONICAL_FIXES[name]
    (project / "module.py").write_text(fixed, encoding="utf-8")
    ok, output = server._repo_repair_pytest(project, timeout=60)
    assert ok, "canonical fix for %s must pass: %s" % (name, output)
