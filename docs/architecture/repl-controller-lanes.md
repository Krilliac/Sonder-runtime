# REPL durable lane conversations

The terminal follows the existing quiet-instrument design in app/DESIGN.md:
plain text states, compact hierarchy, no animation or color-dependent meaning.
`/agents` retains its legacy live-activity view. `/lanes` reads and controls the
same durable conversations used by the agent UI and standalone controller.

```
/lanes list [cursor]
/lanes show <lane-id> [cursor]
/lanes message <lane-id> <text>
/lanes interrupt|resume|cancel <lane-id>
/lanes reports <lane-id> [cursor]
/lanes ack <lane-id> <report-id> [reports-page-cursor]
/lanes help
```

Show renders a bounded event transcript, retaining multiline text and wrapping
to the terminal width. Long event/report content is explicitly truncated. IDs,
status and revision come from service receipts; recording a request does not
claim that the worker has acknowledged it or stopped an in-flight effect.
List, show and reports distinguish no records on this page, unavailable service,
unknown identifiers, and access refusal. Next-page commands use service cursors.

List pages contain at most 20 source rows. Each row is authorized by the
service, then checked against current configured workspace roots. The lightweight
metadata path loads no mailbox bodies or events; show requests one bounded event
page. Unread report counts use SQL aggregation across all retained reports. Hidden rows
still advance the source cursor and the display names that filtering. Report
pages discover the actual parent from an authorized selected lane, filter its
parent report stream to that lane, and retain that stream's cursor. A filtered
page may be empty while a later page has records. Use the printed ack command,
which includes the report page cursor; an ID outside that bounded page requires
the matching page cursor. The current REPL chat is never assumed to be a parent.

The injected LaneConsoleFacade owns this family of commands; runtime._application
supplies production composition. Console context has local user authorship and
only existing configured workspace roots. No MCP/model parent bearer is accepted
or exposed. Message, interrupt, resume, cancel and ack pass the existing
permission decision engine with exact action/target/content/grant arguments.
Approval identity excludes the fresh host dispatch command ID, so an unchanged
unattended retry consumes an operator-issued one-shot approval. Each actual
dispatch still retains its unique idempotency ID.
The immutable serialized command that produced approval is the one executed.
Roots and request authority are rechecked after approval. Oversized confirmation
detail is refused instead of silently approving undisplayed content.

Read forms remain available in plan mode. Mutations are denied there. An
interactive ask displays bounded sanitized exact arguments and requires the
existing console confirmation. Piped input and JSONL sessions never consume the
next input line as an approval: unattended policy applies, and an ask without an
operator is refused. Existing one-shot approval mechanisms remain the policy
engine's concern; this slice does not mint capabilities or change permission mode.

Terminal output escapes ANSI/OSC, C0/C1, surrogate and Unicode format controls,
including bidirectional overrides; literal line breaks and indentation survive.
The existing JSONL writer remains the sole output stream adapter, so every line
continues to use sonder.repl-output.v1. No terminal chrome is added in JSONL mode.
