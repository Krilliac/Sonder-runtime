"""Unit tests for Spanda Exact-Match Normalized Entropy (R_sc)."""
from __future__ import annotations

import math

import pytest

from sonder_runtime.platform.spanda_rsc import compute_rsc, normalize_answer
from sonder_runtime.platform.config import ConfigError, load_config
from sonder_runtime.platform.spanda_http import (
    evaluate_samples,
    resolve_spanda_policy,
    response_headers,
    uncertainty_error_body,
)
from sonder_runtime.platform.spanda_config import SpandaConfig


def test_normalize_casefold_strip_punct_space():
    assert normalize_answer("  Paris! ") == "paris"
    assert normalize_answer("paris.") == "paris"
    assert normalize_answer("PARIS") == "paris"


def test_compute_rsc_unanimous_consensus_is_zero():
    res = compute_rsc(["Paris", "paris.", "Paris", "Paris", "Paris"])
    assert res["rsc"] == 0.0
    assert res["n_clusters"] == 1
    assert res["w_max"] == 1.0
    assert normalize_answer(res["dominant_answer"]) == "paris"


def test_compute_rsc_all_distinct_is_high():
    samples = ["Berlin", "Rome", "Madrid", "London", "Paris"]
    res = compute_rsc(samples)
    # H_norm = 1 (uniform over K), w_max = 1/K, R_sc = 0.5*1 + 0.5*(1-1/K) = 0.5 + 0.4 = 0.9
    assert res["n_clusters"] == 5
    assert res["rsc"] == 0.9


def test_compute_rsc_alpha_weighting():
    samples = ["A", "A", "B"]
    # clusters: A w=2/3, B w=1/3; K=3
    # H = -(2/3 ln 2/3 + 1/3 ln 1/3) / ln 3
    w = [2 / 3, 1 / 3]
    h = sum(-p * math.log(p) for p in w) / math.log(3)
    expected = 0.25 * h + 0.75 * (1 - 2 / 3)
    res = compute_rsc(samples, alpha=0.25)
    assert abs(res["rsc"] - round(expected, 4)) < 1e-9


def test_compute_rsc_rejects_empty():
    with pytest.raises(ValueError):
        compute_rsc([])


def test_spanda_config_defaults_off(tmp_path=None):
    config = load_config(env={})
    assert config.spanda.enabled is False
    assert config.spanda.k == 3
    assert config.spanda.threshold == 0.35
    assert config.spanda.block is False
    assert config.spanda.alpha == 0.5


def test_spanda_toml_loads(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        """
[spanda]
enabled = true
k = 5
threshold = 0.4
block = true
alpha = 0.6
sample_temperature = 0.3
""",
        encoding="utf-8",
    )
    config = load_config(path, env={"SONDER_API_KEY": "x" * 24})
    assert config.spanda.enabled is True
    assert config.spanda.k == 5
    assert config.spanda.threshold == 0.4
    assert config.spanda.block is True
    assert config.spanda.alpha == 0.6
    assert config.spanda.sample_temperature == 0.3


def test_spanda_toml_rejects_bad_k(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text("[spanda]\nk = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path, env={"SONDER_API_KEY": "x" * 24})
    assert "[spanda].k" in "\n".join(exc.value.errors)


def test_resolve_policy_header_enables_without_config():
    policy = resolve_spanda_policy(SpandaConfig(), {"X-Sonder-Spanda": "1"})
    assert policy.active is True
    policy_off = resolve_spanda_policy(SpandaConfig(), {})
    assert policy_off.active is False


def test_evaluate_and_headers():
    evaluation = evaluate_samples(
        ["yes", "Yes!", "no"],
        alpha=0.5,
        threshold=0.35,
    )
    assert evaluation["n_clusters"] == 2
    assert evaluation["uncertain"] in (True, False)
    headers = response_headers(evaluation)
    assert "X-Sonder-Spanda-Rsc" in headers
    assert "X-Sonder-Spanda-Clusters" in headers
    body = uncertainty_error_body(evaluation, threshold=0.35, correlation_id="abc")
    assert body["error"]["code"] == "SPANDA_RSC_UNCERTAIN"
    assert body["error"]["correlation_id"] == "abc"
