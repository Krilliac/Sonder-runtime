"""Thinking controls live in the domain; root names stay aliases or delegates."""
import server
from sonder_runtime.domain import thinking_controls as controls


def test_root_names_are_aliases_and_the_budget_delegate_stays_in_server():
    assert server._THINK_OPTION_UNSUPPORTED_RE is controls.THINK_OPTION_UNSUPPORTED_RE
    assert server._think_option_unsupported is controls.think_option_unsupported
    assert server._cloud_can_disable_thinking is controls.cloud_can_disable_thinking
    assert server._with_local_thinking_budget is controls.with_local_thinking_budget
    assert server.LOCAL_THINKING_MIN_NUM_PREDICT is controls.LOCAL_THINKING_MIN_NUM_PREDICT
    assert controls.LOCAL_THINKING_MIN_NUM_PREDICT == 4096
    assert server._ensure_cloud_prediction_budget.__module__ == server.__name__


def test_think_option_refusals_are_recognized_narrowly():
    assert controls.think_option_unsupported("think option is not supported by this model")
    assert controls.think_option_unsupported("model does not support thinking")
    assert not controls.think_option_unsupported("context length exceeded")
    assert not controls.think_option_unsupported(None)


def test_only_the_reviewed_hosted_models_accept_think_false():
    assert controls.cloud_can_disable_thinking("glm-5.2:cloud")
    assert controls.cloud_can_disable_thinking(" Kimi-K2.7-Code:cloud ")
    assert not controls.cloud_can_disable_thinking("kimi-k3:cloud")
    assert not controls.cloud_can_disable_thinking("")


def test_local_thinking_budget_returns_a_copy_with_room_for_the_answer():
    payload = {"model": "m", "options": {"num_predict": 256, "temperature": 0}}
    out = controls.with_local_thinking_budget(payload)
    assert out["options"] == {"num_predict": 4096, "temperature": 0}
    assert payload["options"]["num_predict"] == 256
    assert out is not payload
    assert controls.with_local_thinking_budget({"model": "m"}) == {"model": "m"}
    for untouched in (0, -1, 8192, "64", None):
        original = {"options": {"num_predict": untouched}}
        assert controls.with_local_thinking_budget(original) == original
    raised = controls.with_local_thinking_budget({"options": {"num_predict": 10}}, 100)
    assert raised["options"]["num_predict"] == 100


def test_cloud_thinking_policy_applies_per_model_controls_through_the_injected_budget():
    calls = []

    def budget(payload):
        calls.append(dict(payload))

    def apply(model, compact=False):
        payload = {"model": model}
        controls.apply_cloud_thinking_policy(payload, model, compact=compact, ensure_prediction_budget=budget)
        return payload

    assert apply("kimi-k3:cloud") == {"model": "kimi-k3:cloud", "think": True}
    assert apply("kimi-k3:cloud", compact=True) == {"model": "kimi-k3:cloud", "think": True}
    assert apply("glm-5.2:cloud") == {"model": "glm-5.2:cloud", "think": "high"}
    assert apply("glm-5.2:cloud", compact=True) == {"model": "glm-5.2:cloud", "think": False}
    assert apply("kimi-k2.7-code:cloud") == {"model": "kimi-k2.7-code:cloud", "think": True}
    assert apply("kimi-k2.7-code:cloud", compact=True) == {"model": "kimi-k2.7-code:cloud", "think": False}
    assert apply("gpt-oss:120b") == {"model": "gpt-oss:120b", "think": "low"}
    assert apply("custom:latest") == {"model": "custom:latest"}
    assert len(calls) == 3


def test_root_delegate_uses_the_server_budget_seam(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_ensure_cloud_prediction_budget", lambda payload: seen.append(payload))
    payload = {"model": "glm-5.2:cloud"}
    server._apply_cloud_thinking_policy(payload, "glm-5.2:cloud")
    assert payload["think"] == "high"
    assert seen == [payload]
