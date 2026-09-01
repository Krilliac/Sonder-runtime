"""Data-only workload profiles for the generic compute scheduler."""
from __future__ import annotations

from dataclasses import dataclass

from .compute_fabric import ComputeCapability, WorkloadKind


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    kind: WorkloadKind
    all_capabilities: frozenset[ComputeCapability] = frozenset()
    any_capabilities: frozenset[ComputeCapability] = frozenset()
    requires_workspace: bool = False
    requires_deadline: bool = False
    requires_catalog_entry: bool = True
    requires_digest_bound_input: bool = False
    background_preferred: bool = False


_PROFILES = {
    WorkloadKind.BUILD: WorkloadProfile(
        WorkloadKind.BUILD,
        all_capabilities=frozenset({ComputeCapability.CPU}),
        any_capabilities=frozenset({
            ComputeCapability.CMAKE,
            ComputeCapability.MSVC,
            ComputeCapability.CLANG,
        }),
        requires_workspace=True,
    ),
    WorkloadKind.TEST: WorkloadProfile(
        WorkloadKind.TEST,
        all_capabilities=frozenset({ComputeCapability.CPU}),
        requires_workspace=True,
    ),
    WorkloadKind.INDEX: WorkloadProfile(
        WorkloadKind.INDEX,
        any_capabilities=frozenset({ComputeCapability.CLANGD}),
        requires_workspace=True,
        background_preferred=True,
    ),
    WorkloadKind.ANALYSIS: WorkloadProfile(
        WorkloadKind.ANALYSIS,
        any_capabilities=frozenset({
            ComputeCapability.CLANG_TIDY,
            ComputeCapability.CLANG,
        }),
        requires_workspace=True,
    ),
    WorkloadKind.FUZZ: WorkloadProfile(
        WorkloadKind.FUZZ,
        all_capabilities=frozenset({ComputeCapability.CPU}),
        requires_workspace=True,
        requires_deadline=True,
    ),
    WorkloadKind.EMBEDDING: WorkloadProfile(
        WorkloadKind.EMBEDDING,
        any_capabilities=frozenset({
            ComputeCapability.EMBEDDINGS,
            ComputeCapability.OLLAMA,
            ComputeCapability.LLAMACPP,
        }),
        requires_catalog_entry=False,
    ),
    WorkloadKind.TRAINING: WorkloadProfile(
        WorkloadKind.TRAINING,
        all_capabilities=frozenset({ComputeCapability.RAM}),
        any_capabilities=frozenset({ComputeCapability.CUDA, ComputeCapability.QLORA}),
        requires_workspace=True,
        requires_deadline=True,
    ),
    WorkloadKind.RENDER: WorkloadProfile(
        WorkloadKind.RENDER,
        all_capabilities=frozenset({ComputeCapability.BLENDER}),
        requires_workspace=True,
    ),
    WorkloadKind.ENCODE: WorkloadProfile(
        WorkloadKind.ENCODE,
        any_capabilities=frozenset({ComputeCapability.FFMPEG}),
        requires_workspace=True,
    ),
    WorkloadKind.SERVICE: WorkloadProfile(
        WorkloadKind.SERVICE,
        requires_workspace=True,
        requires_deadline=True,
    ),
    WorkloadKind.CONTAINER: WorkloadProfile(
        WorkloadKind.CONTAINER,
        any_capabilities=frozenset({
            ComputeCapability.DOCKER,
            ComputeCapability.PODMAN,
        }),
        requires_workspace=True,
        requires_digest_bound_input=True,
    ),
    WorkloadKind.STORAGE: WorkloadProfile(
        WorkloadKind.STORAGE,
        all_capabilities=frozenset({ComputeCapability.STORAGE}),
        requires_workspace=True,
        background_preferred=True,
    ),
}


def profile_for(kind: WorkloadKind) -> WorkloadProfile:
    if not isinstance(kind, WorkloadKind):
        raise ValueError("workload profile kind is invalid")
    if kind is WorkloadKind.INFERENCE:
        raise ValueError("inference placement remains owned by the model gateway")
    return _PROFILES[kind]


__all__ = ["WorkloadProfile", "profile_for"]
