from dataclasses import replace
import pytest

from sonder_runtime.application.ports.app_managed_work import (
    WorkSpec,
    PreparedWorkbenchRun,
)


def spec(**changes):
    return replace(
        WorkSpec("Inspect and repair the parser", "code", "model:sha256", 12, False),
        **changes
    )


def test_spec_digest_binds_exact_task_and_resolved_execution_options():
    original = spec()
    assert original.digest == spec().digest
    for changes in (
        {"prompt": "Inspect and repair the parser\n"},
        {"tier": "reasoning"},
        {"resolved_model": "other:sha256"},
        {"max_steps": 13},
        {"allow_web": True},
    ):
        assert spec(**changes).digest != original.digest
    assert original.prompt not in repr(original)


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": ""},
        {"prompt": "x" * 16385},
        {"prompt": "a\x00b"},
        {"prompt": "\ud800"},
        {"tier": ""},
        {"resolved_model": ""},
        {"max_steps": True},
        {"max_steps": 0},
        {"max_steps": 65},
        {"allow_web": 1},
    ],
)
def test_spec_rejects_unbounded_or_ambiguous_values(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def test_task_preserves_newlines_and_unicode_without_normalization():
    value = spec(prompt="Fix café parser.\n\tKeep the existing format.")
    assert value.prompt == "Fix café parser.\n\tKeep the existing format."


def test_prepared_plan_binds_root_ladder_and_policy(tmp_path):
    root = str(tmp_path.resolve())
    plan = PreparedWorkbenchRun(spec(), root, ("model:sha256",), "a" * 64, False)
    assert plan.project_root not in repr(plan)
    for changes in (
        {"policy_digest": "b" * 64},
        {"allow_location": True},
        {"model_ladder": ("model:sha256", "next:model")},
    ):
        assert replace(plan, **changes).digest != plan.digest
    with pytest.raises(ValueError):
        replace(plan, model_ladder=("wrong:model",))
    with pytest.raises(ValueError):
        replace(plan, project_root="relative")
    with pytest.raises(ValueError):
        replace(plan, allow_location=1)
