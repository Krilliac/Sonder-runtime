"""``[batch_jobs]`` is typed, bounded and wired like ``[playbooks]``."""
from dataclasses import replace

import pytest

from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config


def _errors(**changes):
    from sonder_runtime.platform.batch_jobs_config import BatchJobsConfig, batch_jobs_errors

    return batch_jobs_errors(replace(SonderConfig(), batch_jobs=replace(BatchJobsConfig(), **changes)))


def test_defaults_are_valid_and_visible_in_the_redacted_dict():
    config = SonderConfig()
    assert config.batch_jobs.max_consecutive_failures == 5
    assert config.batch_jobs.order == "stage_major"
    assert config.as_redacted_dict()["batch_jobs"]["max_items_per_job"] == 5000
    assert _errors() == []


def test_toml_overrides_apply(tmp_path):
    path = tmp_path / "batch.toml"
    path.write_text('[batch_jobs]\nmax_stages = 3\norder = "item_major"\neta_window = 10\n', encoding="utf-8")
    config = load_config(path, env={})
    assert (config.batch_jobs.max_stages, config.batch_jobs.order, config.batch_jobs.eta_window) == (3, "item_major", 10)


@pytest.mark.parametrize("field,value", [
    ("max_items_per_job", 0), ("max_items_per_job", 100001), ("max_stages", 17),
    ("max_items_per_add", 5001), ("max_item_input_chars", 255), ("max_item_input_chars", 40001),
    ("max_rendered_prompt_chars", 40001), ("answer_max_chars", 1023), ("answer_max_chars", 1000001),
    ("default_item_timeout_seconds", 4), ("max_item_timeout_seconds", 3601),
    ("max_active_jobs", 0), ("max_active_jobs", 65), ("max_consecutive_failures", 0),
    ("eta_window", 1), ("eta_min_samples", 0), ("eta_min_samples", 21),
    ("order", "random"), ("enabled", 1), ("max_stages", True), ("max_stages", 2.0),
    ("retention_ttl_seconds", 60), ("retention_ttl_seconds", 31536001), ("retention_ttl_seconds", -1),
])
def test_out_of_range_values_are_rejected(field, value):
    assert _errors(**{field: value})


def test_zero_retention_means_keep_and_default_timeout_cannot_exceed_the_cap():
    assert _errors(retention_ttl_seconds=0) == []
    assert _errors(default_item_timeout_seconds=600, max_item_timeout_seconds=300)


def test_invalid_toml_fails_before_runtime(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[batch_jobs]\nmax_active_jobs = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="batch_jobs"):
        load_config(path, env={})
