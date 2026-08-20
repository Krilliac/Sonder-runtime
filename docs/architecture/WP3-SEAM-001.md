# WP3 SEAM-001 — ModelGateway contract

## Boundary

Added the provider-neutral application port
`sonder_runtime/application/ports/model_gateway_contract.py`. It defines
typed immutable generation messages and requests, final generation results,
ordered streaming chunks, capability identifiers, point-in-time capability
health, and the `ModelGatewayProvider` protocol.

Providers expose separate `generate`, `stream`, and `capability_health`
operations. Streaming is a single-consumer ordered iterable; optional usage
and finish data remain provider facts. Health is explicit and capability-scoped
so callers can distinguish an unhealthy provider from one that is healthy but
does not support streaming.

## Scope

This slice is additive. Existing gateway ports, adapters, composition roots,
call sites, and transports are unchanged. No provider is wired to the new
protocol yet; migration and adapter conformance are follow-up work.

## Evidence

- `tests/test_model_gateway_contract_wp3.py` covers immutable typed requests,
  required-value validation, capability health, protocol shape, and a fixture
  provider exercising generation and streaming.
- Focused gate: `python -m pytest tests/test_model_gateway_contract_wp3.py`.
- No specification checkbox, commit, or push is part of this slice.
