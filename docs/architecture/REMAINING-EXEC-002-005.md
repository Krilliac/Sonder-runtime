# EXEC-002 / EXEC-005 — guarded container and remote worlds

`world_providers.py` supplies explicit container and remote world identities.
Both providers fail closed until an injected worker and opt-in configuration
are present. The reference layer deliberately reports isolation as
unverified; deployment-specific adapters must provide evidence before making
stronger claims. Focused coverage is in `test_remaining_execution_providers.py`.
