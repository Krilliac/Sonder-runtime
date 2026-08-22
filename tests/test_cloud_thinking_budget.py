from sonder_runtime.domain import cloud_thinking_budget


def test_budget_policy_owns_implementation_and_preserves_root_alias():
    import server

    assert server._ensure_cloud_prediction_budget.__module__ == server.__name__
    assert cloud_thinking_budget.ensure_prediction_budget.__module__ == cloud_thinking_budget.__name__


def test_undersized_positive_budget_is_raised_without_mutating_options_in_place():
    options = {"num_predict": 128, "temperature": 0.2}
    payload = {"options": options}

    cloud_thinking_budget.ensure_prediction_budget(payload, minimum=4096)

    assert payload["options"] == {"num_predict": 4096, "temperature": 0.2}
    assert payload["options"] is not options
    assert options == {"num_predict": 128, "temperature": 0.2}


def test_missing_invalid_unlimited_and_generous_budgets_are_unchanged():
    cases = (
        {},
        {"num_predict": "128"},
        {"num_predict": 0},
        {"num_predict": -1},
        {"num_predict": 4096},
        {"num_predict": 8192},
    )
    for options in cases:
        payload = {"options": dict(options)}
        before = dict(payload["options"])
        cloud_thinking_budget.ensure_prediction_budget(payload)
        assert payload["options"] == before


def test_non_mapping_options_are_ignored():
    for value in (None, [], "options"):
        payload = {"options": value}
        cloud_thinking_budget.ensure_prediction_budget(payload)
        assert payload == {"options": value}
