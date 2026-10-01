# File tools in chat

Clear local file questions now pass through `intents.classify_execution`,
`ChatLaneService`, and the existing served work admission/receipt path. They
select `inspection`, a bounded read-only agent using exactly
`workspace_inventory`, `directory_tree`, `file_find`, `text_search`, `file_read`,
`file_read_range`, and `repository_symbol_index`. The agent must obtain file
evidence before answering. It has no write, execution, web, or delegation tools.

Clear file creation/edit requests use the existing workbench. An explicit
project must resolve inside the already authorized file roots. Default/unset
projects use a fresh `<state-home>/creations/chat-<uuid>/` directory. Its creation
passes the existing permission gate and typed directory tool; every subsequent
tool call retains the normal permission, consent, and project-path checks.
An unknown project name refuses instead of resolving against the process cwd.
Default **inspection** retains the read tools' existing runtime-workspace root;
it never uses that fallback for a write. Neither route runs in unsafe-lab mode.

The HTTP `sonder_receipt.chat_work` retains its existing schema and names the
requested mode and routing reason. The rendered route header and result name
the selected project. Concrete model selections can use these bounded file
routes without changing models; other pinned-model requests, structured output,
and Responses API requests retain their previous routing. Developer authority
is still required at the existing served work boundary.

Conceptual questions, negations, explicit no-tools requests, and uncertain
requests stay in ordinary chat. Existing fleet, persistent-work, plan-only,
and compound-work classification keeps its precedence. The fallback no-tools
system guidance is added only when `classify_file_intent` recognizes a file
request that reaches the default local chat fallback. Explicit-model and cloud
bypasses, ordinary conversation, and explicit no-tools requests retain their
original system prompt. It names the available tools and requires honest reporting. The HTTP
plain-answer post-check appends the requested unsaved-file note for unsupported
save claims or instructions to run an unwritten path. Tool-lane answers are not
passed through that no-tools check.

The `/delegate` spelling is recognized here only as a clear file-work request
when it falls through the slash router. A dedicated `/delegate` handler has
precedence when integrated; this change does not implement its command or UI.
