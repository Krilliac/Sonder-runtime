Create a short executable plan for this autonomous goal.
Objective: $objective
Project: $project
Policy: $policy
Web: $web
Adaptive checkpoints: $adaptive
Initial task limit: $initial_limit
Overall task ledger limit: $max_tasks
Replan budget: $max_replans
Allowed tools: $tools

Use measurable success criteria. Order inspection before mutation and always finish with grounded validation. Under observe policy, do not create implementation tasks. Keep the initial plan within its smaller limit so adaptive review has room to replace stale pending work. JSON schema:
{"summary":"...","success_criteria":["..."],"tasks":[{"title":"...","kind":"inspect|research|implement|validate|report","instruction":"specific bounded action"}]}
