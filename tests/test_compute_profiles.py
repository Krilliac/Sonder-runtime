from __future__ import annotations

import pytest

from sonder_runtime.domain.compute_fabric import ComputeCapability, WorkloadKind
from sonder_runtime.domain.compute_profiles import profile_for


def test_every_non_inference_workload_has_a_bounded_profile() -> None:
    non_inference = set(WorkloadKind) - {WorkloadKind.INFERENCE}
    assert {profile_for(kind).kind for kind in non_inference} == non_inference
    assert profile_for(WorkloadKind.FUZZ).requires_deadline
    assert ComputeCapability.FFMPEG in profile_for(WorkloadKind.ENCODE).any_capabilities
    assert ComputeCapability.BLENDER in profile_for(WorkloadKind.RENDER).all_capabilities
    assert profile_for(WorkloadKind.SERVICE).requires_catalog_entry
    assert profile_for(WorkloadKind.CONTAINER).requires_digest_bound_input


def test_profiles_distinguish_workspace_and_external_tool_requirements() -> None:
    assert profile_for(WorkloadKind.BUILD).requires_workspace
    assert ComputeCapability.CMAKE in profile_for(WorkloadKind.BUILD).any_capabilities
    assert ComputeCapability.CLANGD in profile_for(WorkloadKind.INDEX).any_capabilities
    assert profile_for(WorkloadKind.STORAGE).all_capabilities == frozenset(
        {ComputeCapability.STORAGE}
    )


def test_inference_profile_is_rejected_in_favor_of_model_gateway() -> None:
    with pytest.raises(ValueError, match="model gateway"):
        profile_for(WorkloadKind.INFERENCE)
