# WP1 Two-Hundred-Twenty-Sixth Slice — system-profile boolean policy ownership

## Boundary

Moved the historical boolean environment-name override policy used by
`sonder_runtime.platform.system_profile` into the canonical
`sonder_runtime.platform.config_environment` boundary as
`env_bool_from_env`. The system-profile module retains its `_env_bool`
compatibility alias, preserving existing NPU, CUDA, and ROCm override behavior
and monkeypatch surfaces. This slice is limited to the system-profile platform
seam; launcher health and the root hardware recommender are unchanged.

## Evidence

- `tests/test_config_environment_ownership.py` verifies canonical identity and
  the system-profile compatibility alias for truthy, default, and false values.
- `python -m pytest tests/test_config_environment_ownership.py tests/test_npu_profile.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime system_profile.py` passes.
- `git diff --check` passes.
