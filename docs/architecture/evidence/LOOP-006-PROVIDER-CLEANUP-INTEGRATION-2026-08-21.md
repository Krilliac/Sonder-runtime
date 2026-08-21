# LOOP-006 provider cleanup integration — 2026-08-21

## Scope

This bounded slice connects the typed `ProcessTreeSupervisor` contract to an
explicit local execution-provider registry. It does not change the general
capability registry or any provider outside the local execution cleanup seam.

## Implemented contract

- `LocalExecutionProviderRegistry` preserves registration order and rejects
  duplicate or lifecycle-incomplete providers.
- `cancel_and_cleanup` sends cancellation and provider cleanup to every
  registered local provider, even when another provider is unsupported or
  returns an incomplete result.
- A registered typed process-request factory routes through
  `ProcessTreeSupervisor` and preserves its `ProcessTreeCleanupReceipt`.
- Providers without a typed process request receive an explicit incomplete,
  `requested=False` receipt. Unsupported process cleanup is never reported as
  complete.
- Provider cleanup and supervisor exceptions are converted into fail-closed
  incomplete receipts, while remaining providers still receive cleanup.
- Aggregate completion requires cancellation, provider quiescence/resource
  release, and process-tree completion.

## Evidence

`tests/test_loop006_provider_cleanup.py` covers two-provider fan-out,
unsupported providers, incomplete provider/process cleanup, and malformed
typed requests. The focused suite passed with 4 tests.

## Remaining boundary

This registry is an explicit local-execution orchestration seam. Existing
generic and remote providers remain outside it unless they are registered with
the local execution contract and a typed process-request factory.
