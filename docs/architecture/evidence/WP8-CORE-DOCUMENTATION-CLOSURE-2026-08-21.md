# WP8 Core Invariants and Documentation Closure

Date: 2026-08-21

## Core invariants

The runtime is a local, single-operator, modular monolith. Typed application ports and packaged adapters preserve data while root compatibility modules remain bounded migration shims. Startup capabilities and authority are explicit, training is attended and gated, deployments and update manifests are immutable/signed, and no external framework is treated as an authority. These invariants are exercised by the runtime configuration, authority, update, training, provider, extension, and architecture suites.

## Documentation closure

The architecture README identifies the authority index and master specification; historical program documents and ADR directories are labeled; new ADRs have one canonical namespace; focused contracts and generated references are cataloged; evidence/status projections are generated and checked; and stale-promise checks are part of the documentation gate.

## Verification

Focused command: `python -m pytest tests/test_runtime_model_configuration.py tests/test_cloud_model_policy.py tests/test_admin_auth_secret.py tests/test_update_manifest_trust.py tests/test_attended_training_execution.py tests/test_read_only_agent_policy.py tests/test_model_gateway_factory.py tests/test_extension_registry.py tests/production/test_entrypoint.py --basetemp C:\Users\Nathan\Documents\Codex\pytest-wp8-core-docs -q`

Result: 104 passed, 7 expected skips, and 1 known pytest cache-permission warning.

The entries in this checkpoint are `implemented_unverified`: repository-level invariants and gates are present, but no claim is made that a particular external production deployment or CI host is already configured.
