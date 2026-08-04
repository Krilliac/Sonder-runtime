"""Offline tests for scripts/benchmark_moat.py.

Everything here runs with a scripted fake ``model_fn`` — no model, no network,
no GPU, no sleeps. The fake decides its answer purely from the prompt text it
receives, which is exactly what lets us prove the harness (a) runs all three
arms, (b) computes per-arm pass-rates and the deltas, (c) renders the Markdown
scorecard, and (d) reflects both "warming helps" and "warming does no harm".
"""
import importlib.util
import os
import sys

import pytest

# scripts/ is not a package; load the module by path so the test is independent
# of how the runner is invoked.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "benchmark_moat.py",
)
_spec = importlib.util.spec_from_file_location("benchmark_moat", _MODULE_PATH)
bm = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass string-annotation resolution can find the
# module (dataclasses looks it up in sys.modules by __module__).
sys.modules["benchmark_moat"] = bm
_spec.loader.exec_module(bm)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------
LESSON = "the secret answer is 42"


def _helps_task():
    """A task the bare model gets wrong but a warmed lesson makes it get right."""
    return bm.MoatTask(
        name="needs_lesson",
        prompt="What is the secret answer?",
        grade=bm.grade_contains("42"),
        lessons=[LESSON],
    )


def _already_solved_task():
    """A task the model always answers correctly, warming or not."""
    return bm.MoatTask(
        name="always_ok",
        prompt="Say hello.",
        grade=bm.grade_contains("hello"),
    )


def make_lesson_following_fake():
    """Fake model that only emits '42' when the secret lesson is in the prompt,
    and always greets. Deterministic: output depends solely on the prompt.
    """
    def _fn(prompt):
        out = "hello there"
        if LESSON in prompt:
            out += " -- the answer is 42"
        return out
    return _fn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_runs_all_three_arms():
    tasks = [_helps_task(), _already_solved_task()]
    result = bm.benchmark(tasks, make_lesson_following_fake())
    assert set(result["arms"].keys()) == set(bm.ARMS)
    for arm in bm.ARMS:
        assert result["arms"][arm]["total"] == 2
        assert len(result["arms"][arm]["tasks"]) == 2
    assert result["n_tasks"] == 2


def test_warming_helps_pass_rate_and_delta():
    tasks = [_helps_task(), _already_solved_task()]
    result = bm.benchmark(tasks, make_lesson_following_fake())
    bare = result["arms"][bm.ARM_BARE]
    cold = result["arms"][bm.ARM_RUNTIME_COLD]
    warm = result["arms"][bm.ARM_RUNTIME_WARM]

    # Bare and cold: only the always-ok task passes (1/2). Warm: both pass (2/2).
    assert bare["passed"] == 1
    assert cold["passed"] == 1
    assert warm["passed"] == 2
    assert bare["pass_rate"] == 0.5
    assert warm["pass_rate"] == 1.0

    # The headline moat delta is strictly positive; the do-no-harm delta is zero.
    assert result["deltas"]["warm_vs_bare"]["pass_rate"] == pytest.approx(0.5)
    assert result["deltas"]["cold_vs_bare"]["pass_rate"] == pytest.approx(0.0)
    assert result["deltas"]["warm_vs_cold"]["pass_rate"] == pytest.approx(0.5)


def test_warming_does_no_harm_when_it_does_not_help():
    # Every task is already solved regardless of augmentation. Warming must not
    # drop the pass-rate: the moat delta is exactly 0, not negative.
    tasks = [_already_solved_task(), _already_solved_task()]
    result = bm.benchmark(tasks, make_lesson_following_fake())
    assert result["arms"][bm.ARM_BARE]["pass_rate"] == 1.0
    assert result["arms"][bm.ARM_RUNTIME_WARM]["pass_rate"] == 1.0
    assert result["deltas"]["warm_vs_bare"]["pass_rate"] == pytest.approx(0.0)
    assert result["deltas"]["warm_vs_bare"]["mean_score"] == pytest.approx(0.0)


def test_cold_arm_equals_bare_prompt():
    # The cold arm must reduce to the bare prompt (empty store injects nothing),
    # so a fake that records prompts should see identical text for both arms.
    seen = {}

    def recording_fake(prompt):
        seen.setdefault(prompt, 0)
        seen[prompt] += 1
        return "hello"

    task = _helps_task()
    bare_prompt = bm._prompt_for_arm(task, bm.ARM_BARE, bm.default_retrieve)
    cold_prompt = bm._prompt_for_arm(task, bm.ARM_RUNTIME_COLD, bm.default_retrieve)
    warm_prompt = bm._prompt_for_arm(task, bm.ARM_RUNTIME_WARM, bm.default_retrieve)
    assert bare_prompt == cold_prompt
    assert warm_prompt != bare_prompt
    assert LESSON in warm_prompt
    # sanity: recording_fake is a valid model_fn shape
    recording_fake(bare_prompt)
    assert seen[bare_prompt] == 1


def test_mean_score_partial_credit():
    # grade_all_of gives fractional credit; mean_score must reflect it while the
    # pass bit stays False below threshold.
    task = bm.MoatTask(
        name="partial",
        prompt="mention foo and bar and baz",
        grade=bm.grade_all_of(["foo", "bar", "baz"]),
    )
    result = bm.benchmark([task], lambda p: "here is foo and bar only")
    arm = result["arms"][bm.ARM_BARE]
    assert arm["tasks"][0]["score"] == pytest.approx(2.0 / 3.0)
    assert arm["tasks"][0]["passed"] is False
    assert arm["mean_score"] == pytest.approx(2.0 / 3.0)
    assert arm["pass_rate"] == 0.0


def test_scorecard_renders_markdown_table():
    tasks = [_helps_task(), _already_solved_task()]
    result = bm.benchmark(tasks, make_lesson_following_fake())
    card = bm.render_scorecard(result)
    assert "# Moat benchmark scorecard" in card
    # header row and all three arm labels present
    assert "| Arm | Pass rate | Mean score |" in card
    assert "bare" in card
    assert "+runtime (cold)" in card
    assert "+runtime (warmed)" in card
    # the moat delta row is rendered
    assert "the moat" in card
    # pass counts appear as N/N
    assert "2/2" in card


def test_determinism_same_fake_same_result():
    tasks = [_helps_task(), _already_solved_task()]
    r1 = bm.benchmark(tasks, make_lesson_following_fake())
    r2 = bm.benchmark(tasks, make_lesson_following_fake())
    assert r1 == r2
    assert bm.render_scorecard(r1) == bm.render_scorecard(r2)


def test_injected_scorer_overrides_task_grader():
    # A scorer passed to benchmark() replaces the per-task grader entirely.
    task = _already_solved_task()  # its own grader would pass on "hello"

    def always_zero(_task, _response):
        return 0.0

    result = bm.benchmark([task], lambda p: "hello", scorer=always_zero)
    assert result["arms"][bm.ARM_BARE]["passed"] == 0
    assert result["arms"][bm.ARM_BARE]["mean_score"] == 0.0


def test_bad_grader_does_not_kill_run():
    # A grader that raises is recorded as score 0 with a detail note, not an
    # exception that aborts the whole arm.
    def boom(_response):
        raise ValueError("nope")

    good = _already_solved_task()
    bad = bm.MoatTask(name="explodes", prompt="x", grade=boom)
    result = bm.benchmark([bad, good], lambda p: "hello")
    rows = {t["name"]: t for t in result["arms"][bm.ARM_BARE]["tasks"]}
    assert rows["explodes"]["score"] == 0.0
    assert rows["explodes"]["passed"] is False
    assert "detail" in rows["explodes"]
    assert rows["always_ok"]["passed"] is True


def test_import_is_side_effect_free():
    # Importing the module must not have required a model or network. As a proxy,
    # DEFAULT_SUITE is bounded and well-formed, and default_model_fn is a builder
    # (callable) that we never invoked here.
    assert 0 < len(bm.DEFAULT_SUITE) <= 12
    names = [t.name for t in bm.DEFAULT_SUITE]
    assert len(names) == len(set(names))
    for t in bm.DEFAULT_SUITE:
        assert callable(t.grade)
        assert t.prompt.strip()
    assert callable(bm.default_model_fn)
