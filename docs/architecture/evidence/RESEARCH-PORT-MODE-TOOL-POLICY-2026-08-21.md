# Research port: explicit mode/tool policy projection

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Continue documents explicit Chat, Plan, and Agent modes with mode-specific tool
availability and Ask First/Automatic/Excluded tool policies. Sonder already
has a stronger permission engine, but lacked a provider-neutral inspectable
projection of those mode semantics.

Source: <https://docs.continue.dev/ide-extensions/agent/how-it-works>

## Implemented slice

`project_mode_tool_policy()` now produces a typed policy where:

- Chat excludes every tool;
- Plan exposes only declared read-only tools automatically;
- Agent exposes read-only tools automatically and marks mutations as
  approval-required;
- unknown or unclassified tools fail closed;
- the policy can be serialized for UI/API inspection.

This is a projection only; the existing permission engine remains the final
execution gate.

Evidence: `tests/test_mode_tool_policy.py`.
