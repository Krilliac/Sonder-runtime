# WP2 LOOP-005 — Steering and order

## Scope

`sonder_runtime.application.loop_steering` defines immutable application-loop
commands and a pure ordering rule. It does not execute commands, change the
existing loop or cancellation modules, emit events, or persist a command.

## Contract

- `SteeringCommand` is a frozen snapshot. Factory methods define five explicit
  kinds: `follow_up`, `immediate_steering`, `passive_context_injection`,
  `cancellation`, and `stop`.
- A follow-up is content for a subsequent turn; it does not interrupt the
  current turn. Immediate steering is content for the current turn's next safe
  boundary. Passive context is context only and must not change control flow.
- Cancellation is the highest-priority command and requests active work to
  stop. Stop is next: it prevents new work while allowing an adapter to apply
  its graceful-drain policy. Neither command is executed by this module.
- `order_commands()` returns a new tuple ordered as cancellation, stop,
  immediate steering, follow-up, passive context injection. Commands with the
  same kind retain admission order by their non-negative `sequence`.
- Ordering is deterministic and side-effect free. Adapters remain responsible
  for safe-boundary timing, cancellation propagation, stop/drain behavior, and
  translating commands into their runtime mechanisms.

## Compatibility boundary

No existing loop, cancellation, event, or repository interface is changed.
This slice contains only a new application contract and focused tests.

## Verification

- `tests/test_loop_steering.py`
- `python -m pytest tests/test_loop_steering.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
