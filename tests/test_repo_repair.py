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
    "aliasing": (
        "def with_defaults(config):\n"
        "    merged = dict(config)\n"
        "    merged.setdefault('retries', 3)\n"
        "    return merged\n"
    ),
    "intdivision": (
        "def average(values):\n"
        "    return sum(values) / len(values)\n"
    ),
    "noneguard": (
        "def name_length(user):\n"
        "    if not user or not user.get('name'):\n"
        "        return 0\n"
        "    return len(user['name'])\n"
    ),
    "wrongscope": (
        "def group_lengths(words):\n"
        "    buckets = {}\n"
        "    for word in words:\n"
        "        buckets.setdefault(len(word), []).append(word)\n"
        "    return buckets\n"
    ),
    "rangeend": (
        "def inclusive_range(start, end):\n"
        "    return list(range(start, end + 1))\n"
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
    ok, output, infra = server._repo_repair_pytest(project, timeout=60)
    assert not infra, "template %s must get a real verdict: %s" % (name, infra)
    assert not ok, "template %s must fail before repair: %s" % (name, output)

    fixed = _CANONICAL_FIXES[name]
    (project / "module.py").write_text(fixed, encoding="utf-8")
    ok, output, infra = server._repo_repair_pytest(project, timeout=60)
    assert not infra, "canonical fix for %s must get a verdict: %s" % (
        name, infra)
    assert ok, "canonical fix for %s must pass: %s" % (name, output)


def test_infrastructure_timeout_is_not_a_model_verdict(tmp_path):
    """A pytest timeout says nothing about the candidate code, so it must be
    reported as an infrastructure error — never recorded as a model failure.
    A memory-starved run once banked 20 bogus 'failed' outcomes this way."""
    project = Path(tmp_path) / "slow"
    project.mkdir()
    (project / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    ok, output, infra = server._repo_repair_pytest(project, timeout=5)
    assert not ok
    assert "timed out" in infra
    assert "timed out" in output


def test_candidate_that_cannot_import_is_the_models_fault(tmp_path):
    """A module the model wrote with a SyntaxError aborts collection (exit 2),
    and the error is reported against the test file that imported it. That is
    the candidate's fault and must stay attributable - excusing it as
    infrastructure is the mirror image of the bug the infra split fixed, and
    it happened live when a generation leaked activity-log text into
    module.py."""
    project = Path(tmp_path) / "broken-candidate"
    project.mkdir()
    (project / "module.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (project / "test_module.py").write_text(
        "from module import f\n\n\ndef test_f():\n    assert f()\n",
        encoding="utf-8",
    )
    ok, _output, infra = server._repo_repair_pytest(project, timeout=60)
    assert not ok
    assert infra == "", "a candidate that cannot import is a model failure"


def test_missing_tests_is_not_a_model_verdict(tmp_path):
    """Exit 5 (no tests collected) means the template never arrived, which
    says nothing about the candidate."""
    project = Path(tmp_path) / "no-tests"
    project.mkdir()
    ok, _output, infra = server._repo_repair_pytest(project, timeout=60)
    assert not ok
    assert "without a test verdict" in infra


def test_campaign_verdict_tokens_cannot_contain_each_other():
    """The campaign pass check is a containment check, so a wrong verdict must
    never contain the expected one as a substring. 'valid'/'invalid' broke
    this and let wrong answers false-pass; ok/bad is the fix."""
    expected = server._campaign_expected("balanced")
    verdicts = sorted(set(expected.split()))
    assert verdicts, "balanced task must declare verdict tokens"
    for a in verdicts:
        for b in verdicts:
            if a != b:
                assert a not in b, (
                    "verdict %r contains %r — wrong answers can false-pass"
                    % (b, a)
                )


def test_pitfall_errors_reach_the_line_the_nightly_runner_keeps():
    """scripts/nightly_self_improve.py logs only _first_line() of a tool
    result, so a pitfall-distillation crash reported on any later line is
    invisible in unattended runs. Two nights of logs carried no trace of a
    crash that replay showed happening on 2 of 10 failures."""
    noisy = server._campaign_headline(20, 24, 20, 4, 3, 12.5)
    first_line = noisy.strip().splitlines()[0]
    assert "3 pitfall-errors" in first_line

    # A healthy run must stay byte-identical, or every existing log grep and
    # eyeball comparison against past nights breaks.
    healthy = server._campaign_headline(24, 24, 24, 0, 0, 12.5)
    assert healthy == (
        "campaign generate/compile/execute/record: "
        "24/24 passed, 24 recorded, 0 failed-recorded in 12.500s"
    )


@pytest.mark.parametrize("languages", [
    ["powershell", "cpp", "csharp"],
    ["python", "javascript"],
    ["python", "javascript", "powershell", "cpp", "csharp"],
    ["cpp"],
])
def test_campaign_covers_every_language_task_pair(languages):
    """Language and task must not advance in lockstep. When their counts share
    a factor, pairing both on the same index pins each language to one residue
    class: 3 languages against 12 tasks gave each language only 4 tasks, and
    PowerShell could never draw the three it fails."""
    tasks = [name for name, _text in server._CAMPAIGN_TASKS]
    total = len(languages) * len(tasks)
    seen = {language: set() for language in languages}
    for index in range(total):
        language = languages[index % len(languages)]
        name = tasks[(index // len(languages)) % len(tasks)]
        seen[language].add(name)
    for language, drawn in seen.items():
        assert drawn == set(tasks), (
            "%s never draws %s" % (language, sorted(set(tasks) - drawn))
        )
