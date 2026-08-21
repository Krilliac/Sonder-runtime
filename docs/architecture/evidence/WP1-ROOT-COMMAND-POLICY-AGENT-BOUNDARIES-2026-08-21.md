# WP1 root command, policy, and agent boundaries — 2026-08-21

This slice moves the production-facing edges for three remaining WP1 legacy
roots behind packaged boundaries while retaining compatibility identities for
old callers:

- `command_router.py` and `slash_menu.py` are compatibility redirects to the
  packaged REPL router/menu implementations.
- The REPL and HTTP surfaces obtain permission actions and gate vocabulary from
  the typed `PermissionPolicy` adapter rather than importing policy constants
  from the root module.
- The runtime container exposes a lazy typed agent-registry factory, so
  composition does not initialize or import the root `master_orchestrator`.

Evidence:

- `sonder_runtime/interfaces/repl/command_router.py`
- `sonder_runtime/adapters/optional_slash_menu_impl.py`
- `sonder_runtime/application/ports/permission_policy.py`
- `sonder_runtime/adapters/security/permission_policy.py`
- `sonder_runtime/adapters/runtime_container.py`
- `tests/test_permission_policy_provider.py`
- `tests/test_wp1_master_orchestrator_boundary.py`
- `tests/test_command_router.py`
- `tests/test_command_router_catalog.py`
- `tests/test_slash_menu.py`
- `tests/test_optional_slash_menu.py`
- `tests/test_repl_catalog.py`
- `tests/test_repl_input.py`

The focused migration run passed 227 tests and `scripts/check_architecture.py`
passed. The root files remain compatibility shims by design; this evidence
does not claim that all legacy roots or all interface behavior have been
retired, nor does it promote formal verification.
