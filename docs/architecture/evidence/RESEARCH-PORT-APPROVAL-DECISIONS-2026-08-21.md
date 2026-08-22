# Research port: durable approval decisions

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

LangGraph documents checkpoint-backed human-in-the-loop interrupts with
approve, edit, and reject decisions. Sonder already has approval event kinds
and permission policy, but lacked a typed contract binding an edited argument
set to the original approval request.

Source: <https://docs.langchain.com/oss/python/langchain/human-in-the-loop>

## Implemented slice

`domain.approval` now provides:

- typed `ApprovalRequest` and `ApprovalDecision` records;
- approve/edit/reject semantics with allowed-decision validation;
- request identity binding and expiry checks;
- bounded JSON edit arguments with deterministic SHA-256 identity;
- `ResolvedApproval` output suitable for a durable executor to apply.

The contract is pure and does not execute tools or bypass the existing
permission policy.

Evidence: `tests/test_approval_envelope.py`.
