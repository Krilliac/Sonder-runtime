Choose the smallest reliable execution mode for this developer-authorized work request. Prefer workbench when the task is self-contained and likely to finish in one bounded tool loop. Prefer autopilot when it has several dependent phases, needs durable progress, or requires discovery followed by implementation and independent validation. Choose fast only for tiny mechanical/read tasks, code for repository/code/tool work, and general for prose-heavy explanation or review.
Project: $project
Request: $request
JSON schema: {"mode":"workbench|autopilot","tier":"fast|code|general","reason":"brief evidence-based reason","confidence":0.0}
