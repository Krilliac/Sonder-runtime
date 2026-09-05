# Bounded recovery HTTP integration

This slice adds runtime-owned recovery handles and explicit HTTP preparation, attachment, verification resume, status and close. Attachment and verifier approval remain separate host-controlled calls. HTTP does not grant approval, and original prepared work and terminal receipts remain immutable.

The initial d336a139 draft failed the bounded HTTP acceptance wait because live private-path checks were repeated and status polling competed with the recovery callback. The correction removes duplicate inventory work within one current guard invocation and avoids a second durable-history admission while a callback is still busy. Each status response still requires fresh current account/control/selection admission. No mutable authorization or path snapshot is cached across callbacks. Closed or ambiguous callbacks are never assumed terminated from timeout alone.

Validation recorded for the correction:

- Fourteen registry, real loopback HTTP and actual isolated owned-slot checks passed. The HTTP path covers original verifier-pending work, fresh login/control, separate attachment and verifier approvals, deliberately lost durable-completion response, observational terminal reconciliation, old-control refusal and revoked-login refusal. One original model and one verifier gateway execution were observed; terminal retries did not replay either.
- Forty-seven final focused, ownership and architecture checks passed, including submit ambiguity before/after enqueue, truthful unknown phase after a late callback, cleanup retention/retry, logout during busy status and changed live workspace roots.

Models and the verifier job gateway in acceptance are scripted. These checks do not prove external model providers, native verifier subprocesses, real multi-node execution or an unrestricted latency guarantee. Daybreak exact-revision review and root integration checks remain required before promotion.

The registry retains at most32 entries, including closed entries, and permits one active callback. There is no eviction or process-restart adoption claim. Public original-output retrieval and recovery UI remain separate incomplete roadmap work.

Reproduce bounded acceptance:

```
python -m pytest tests/test_app_recovery_registry.py tests/test_app_recovery_http.py tests/test_owned_app_recovery_slot.py -q --tb=short
```

This development lane did not modify an installed runtime.
