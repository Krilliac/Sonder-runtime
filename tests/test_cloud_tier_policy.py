from sonder_runtime.domain.cloud_tier_policy import (
    LEGACY_CLOUD_GENERAL_MODEL,
    refresh_live_cloud_tiers,
)


def test_repairs_exact_legacy_binding_from_typed_default():
    tiers = {"cloud-general": LEGACY_CLOUD_GENERAL_MODEL}

    refresh_live_cloud_tiers(
        tiers,
        {},
        default_cloud_general_model="glm-5.2:cloud",
    )

    assert tiers == {"cloud-general": "glm-5.2:cloud"}


def test_preservation_flag_keeps_legacy_binding():
    tiers = {"cloud-general": LEGACY_CLOUD_GENERAL_MODEL}

    refresh_live_cloud_tiers(
        tiers,
        {"SONDER_PRESERVE_LEGACY_CLOUD_GENERAL": "yes"},
        default_cloud_general_model="glm-5.2:cloud",
    )

    assert tiers["cloud-general"] == LEGACY_CLOUD_GENERAL_MODEL


def test_non_legacy_binding_is_not_overwritten():
    tiers = {"cloud-general": "operator:cloud"}

    refresh_live_cloud_tiers(
        tiers,
        {},
        default_cloud_general_model="glm-5.2:cloud",
    )

    assert tiers["cloud-general"] == "operator:cloud"


def test_missing_binding_is_not_created():
    tiers = {}

    refresh_live_cloud_tiers(
        tiers,
        {},
        default_cloud_general_model="glm-5.2:cloud",
    )

    assert tiers == {}
