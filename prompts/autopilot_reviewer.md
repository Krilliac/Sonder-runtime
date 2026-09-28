Review the bounded run and select the next decision.
Objective: $objective
Host gate/issue: $issue
Failures: $failures/$max_failures
Task budget: $task_count/$max_tasks
Adaptive checkpoints: $checkpoints
Replans: $replans/$max_replans
Ledger: $ledger

Use complete only when the host gate says all requirements passed. At an adaptive checkpoint, use continue when the pending plan remains correct, replan only when new evidence makes it stale, or pause when operator judgment is genuinely required. Use retry only after a failure. At every adaptive checkpoint, assess every pending task by ID. A task is stale when completed evidence contradicts its premise or says its work is already unnecessary. A stale task forbids continue: choose replan, omit the contradicted work, and retain necessary validation/reporting. The host preserves tasks marked keep and supersedes only tasks marked stale. Every replan must include only necessary new replacement tasks; tasks may be empty when removing stale work is sufficient and a kept validation task remains. JSON schema:
{"decision":"complete|continue|retry|replan|pause","reason":"...","instruction":"corrected retry instruction or empty","pending_assessment":[{"id":"task-00","verdict":"keep|stale","reason":"evidence comparison"}],"tasks":[{"title":"...","kind":"inspect|research|implement|validate|report","instruction":"..."}]}
