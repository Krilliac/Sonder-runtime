"""Serve-target selection policy lives in the domain; root names are aliases."""
import server
from sonder_runtime.domain import serve_selection


def test_root_names_are_identity_preserving_aliases():
    assert server._allow_cloud_fallback_for_target is serve_selection.allow_cloud_fallback_for_target
    assert server._explicit_serve_selection is serve_selection.explicit_serve_selection


def test_only_configured_tiers_may_use_the_availability_fallback():
    assert serve_selection.allow_cloud_fallback_for_target("cloud")
    assert serve_selection.allow_cloud_fallback_for_target("reasoning")
    assert serve_selection.allow_cloud_fallback_for_target("")
    assert not serve_selection.allow_cloud_fallback_for_target("model:kimi-k3:cloud")
    assert not serve_selection.allow_cloud_fallback_for_target("MODEL:phi4")


def test_explicit_selection_means_a_named_target_or_a_non_default_tier():
    assert serve_selection.explicit_serve_selection("code", "")
    assert serve_selection.explicit_serve_selection("", "phi4:latest")
    assert serve_selection.explicit_serve_selection(" Local ", " gemma3 ")
    assert not serve_selection.explicit_serve_selection("", "")
    assert not serve_selection.explicit_serve_selection("sonder", None)
    assert not serve_selection.explicit_serve_selection(" LOCAL ", "  ")
