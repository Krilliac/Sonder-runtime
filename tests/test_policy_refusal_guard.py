from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.legacy_chat_bridge import chat_request
from sonder_runtime.adapters.model_error_formatting import format_runtime_model_call_error
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceConfig
from sonder_runtime.application.chat.provider_bridge import bind_rung
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.agents.policy_refusal_guard import repeated_policy_refusal


def test_three_identical_raw_policy_refusals_return_detail():
    refusal = "path is outside the approved project"
    observations = [f"ERROR: HOST POLICY: {refusal}"] * 3
    assert repeated_policy_refusal(observations) == refusal


def test_wrapped_observations_compare_across_tools_and_ignore_recovery_text():
    refusal = "host-scoped arguments are not permitted"
    observations = [
        f"step 1 tool=file_read reason=x\nERROR: HOST POLICY: {refusal}\nHOST RECOVERY: x",
        f"step 2 tool=text_search reason=x\nERROR: HOST POLICY: {refusal}\nHOST RECOVERY: y",
        f"step 3 tool=directory_tree reason=x\nERROR: HOST POLICY: {refusal}\nHOST RECOVERY: z",
    ]
    assert repeated_policy_refusal(observations) == refusal


def test_policy_guard_requires_three_matching_trailing_observations():
    refusal = "path is outside the approved project"
    assert repeated_policy_refusal([f"ERROR: HOST POLICY: {refusal}"] * 2) is None
    assert repeated_policy_refusal([
        f"ERROR: HOST POLICY: {refusal}",
        "ERROR: HOST POLICY: another refusal",
        f"ERROR: HOST POLICY: {refusal}",
    ]) is None
    assert repeated_policy_refusal([
        "tool succeeded",
        f"ERROR: HOST POLICY: {refusal}",
        f"ERROR: HOST POLICY: {refusal}",
        f"ERROR: HOST POLICY: {refusal}",
    ]) == refusal


def test_policy_guard_ignores_quoted_error_text_in_a_non_refusal_observation():
    quoted = "the model quoted ERROR: HOST POLICY: path is outside the project"
    assert repeated_policy_refusal([quoted] * 3) is None


def test_successful_observation_with_later_error_text_does_not_trigger():
    success = "file contents\nERROR: HOST POLICY: quoted text"
    assert repeated_policy_refusal([success] * 3) is None


def test_header_reason_does_not_count_as_the_policy_payload():
    refusal = "path is outside the approved project"
    observation = (
        f"step 1 tool=file_read reason=ERROR: HOST POLICY: {refusal}\n"
        "file contents"
    )
    assert repeated_policy_refusal([observation] * 3) is None


def test_multiline_refusals_compare_their_full_continuation():
    first = "ERROR: HOST POLICY: refused path\nallowed roots: project"
    second = "ERROR: HOST POLICY: refused path\nallowed roots: workspace"
    assert repeated_policy_refusal([first, first, second]) is None


def test_success_breaks_a_previous_refusal_streak_and_only_last_three_count():
    refusal = "path is outside the approved project"
    raw = f"ERROR: HOST POLICY: {refusal}"
    assert repeated_policy_refusal([raw, raw, "file contents", raw, raw]) is None
    assert repeated_policy_refusal(["old success", raw, raw, raw]) == refusal


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:11437", "http://127.0.0.1:21437"])
def test_bridged_provider_error_names_provider_and_bound_base_url(base_url):
    """Exercise the actual exception chain; detail-only rewrites leave a false prefix."""
    settings = SonderInferenceConfig(base_url=base_url)

    def generate(request, context):
        raise DependencyUnavailable("connection refused")

    gateway = SimpleNamespace(settings=lambda: settings, generate=generate)
    with bind_rung("sonder_inference", "code") as rung:
        with pytest.raises(ModelCallError) as caught:
            chat_request(
                gateway, {"messages": [{"role": "user", "content": "probe"}]}, rung,
                context=local_owner_context(correlation_id="provider-error-probe"),
            )
    error = caught.value
    assert (error.kind, error.status, error.attempts, error.transient) == (
        "provider_unavailable", 503, 1, False,
    )
    rendered = format_runtime_model_call_error(
        error, endpoint_loopback=False, display="http://127.0.0.1:11434",
    )
    assert rendered.startswith("ERROR contacting sonder_inference at " + base_url), rendered
    assert "Ollama" not in rendered
