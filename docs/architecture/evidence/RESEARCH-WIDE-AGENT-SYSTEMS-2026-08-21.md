# Wide agent-system research for Sonder

Date: 2026-08-21  
Branch: `agent/port-research-findings`

This comparison expands the initial Qwen Sharp Templates, Zero, and Easy
Agent review. It records externally documented patterns and the corresponding
Sonder disposition so future ports remain evidence-driven rather than
feature-shaped duplication.

## Systems reviewed

| System | Documented pattern | Sonder disposition |
| --- | --- | --- |
| OpenHands SDK / Agent Server | Stateless event-driven reasoning/action steps; condensers; security analysis; interchangeable local and remote workspaces; WebSocket event streaming | Existing session/event, compaction, security, and workspace boundaries cover much of this. Prioritize a typed step/interrupt envelope only where current events cannot express it. |
| goose | MCP extensions; portable YAML recipes and subrecipes; ACP server/provider interoperability; parallel subagents; adversary reviewer | Existing MCP, skills, fanout, and subagent surfaces cover the core. Recipe manifests and ACP are candidate future adapters, not a reason to bypass current ports. |
| SWE-agent | Durable `.traj` trajectories and CLI/web inspection for step-by-step debugging and evaluation | Existing session exports and evidence ledgers are adjacent. A trajectory projection that preserves tool/action/result linkage is a high-value next port. |
| Aider | Repository map for context selection; explicit edit formats; separate stronger/cheaper model roles; test-oriented workflow | Existing repository intelligence, context manifests, model routing, and verification cover the design. Measure map selection quality before adding another mapper. |
| Continue | Explicit Chat/Plan/Agent modes; mode-specific tool sets; per-tool Ask First/Automatic/Excluded policy; local rules blocks | Existing command/read-only policy and instruction/skill discovery cover the policy foundation. A typed mode-to-tool policy projection is a candidate port. |
| LangGraph | Checkpointed interrupts; thread IDs as resume cursors; human decisions of approve/edit/reject; streamed interrupt state | Existing durable session repair/checkpoints and permission context cover persistence. The missing candidate is a typed approval decision envelope supporting edited arguments. |
| PydanticAI | Typed dependency injection into prompts/tools/validators; durable execution integrations; streaming and MCP support | Existing composition-root injection and durable session ports cover the architecture. Keep this as a contract review target, not a new framework dependency. |

## Priority order

1. Trajectory projection: expose a bounded, redacted action/observation trace
   that can be inspected and replayed without exposing secrets.
2. Approval decision envelope: represent approve/edit/reject decisions with a
   durable request identity and validated edited arguments.
3. Mode/tool policy projection: make Chat/Plan/Agent tool availability
   inspectable and testable at the existing policy boundary.
4. Recipe/ACP adapters: defer until a concrete host integration needs them;
   existing MCP and workflow contracts are the correct seams.

## Sources

- OpenHands architecture: <https://docs.openhands.dev/sdk/arch/agent>
- OpenHands remote Agent Server: <https://docs.openhands.dev/sdk/guides/agent-server/overview>
- goose architecture: <https://github.com/aaif-goose/goose/blob/main/documentation/docs/goose-architecture/goose-architecture.md>
- SWE-agent trajectory inspector: <https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/inspector.md>
- Aider usage and repo-map: <https://aider.chat/docs/usage.html>
- Continue agent modes and tools: <https://docs.continue.dev/ide-extensions/agent/how-it-works>
- LangGraph human-in-the-loop: <https://docs.langchain.com/oss/python/langchain/human-in-the-loop>
- LangGraph persistence: <https://docs.langchain.com/oss/javascript/langgraph/persistence>
- PydanticAI durable execution: <https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/>
- PydanticAI dependencies: <https://pydantic.dev/docs/ai/core-concepts/dependencies/>
