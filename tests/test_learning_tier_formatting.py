from sonder_runtime.adapters.learning_tier_formatting import format_learning_tiers


def test_formats_local_and_cloud_learning_tiers():
    assert format_learning_tiers(
        {"code": "qwen:latest", "cloud-code": "qwen-cloud:latest"},
        {"code", "cloud-code"},
        cloud_enabled=True,
    ) == (
        "learning tiers\n"
        "  code: on (local, qwen:latest)\n"
        "  cloud-code: on (cloud, qwen-cloud:latest)\n"
        "cloud tiers are available; opt into cloud learning explicitly with "
        "SONDER_LEARN_TIERS"
    )


def test_disables_cloud_tier_when_cloud_policy_is_off():
    assert format_learning_tiers(
        {"fast": "llama:latest", "cloud-general": "general-cloud:latest"},
        {"fast", "cloud-general"},
        cloud_enabled=False,
        cloud_tiers={"cloud-general"},
    ) == (
        "learning tiers\n"
        "  fast: on (local, llama:latest)\n"
        "  cloud-general: disabled (cloud, general-cloud:latest)\n"
        "cloud tiers require SONDER_ALLOW_CLOUD=1; override learning with "
        "SONDER_LEARN_TIERS"
    )


def test_unknown_and_empty_models_are_local_and_rendered_verbatim():
    assert format_learning_tiers(
        {"general": "", "reasoning": None, "custom": "model"},
        (),
        cloud_enabled=False,
    ) == (
        "learning tiers\n"
        "  general: off (local, )\n"
        "  reasoning: off (local, None)\n"
        "  custom: off (local, model)\n"
        "cloud tiers require SONDER_ALLOW_CLOUD=1; override learning with "
        "SONDER_LEARN_TIERS"
    )
