# Editable prompts

Every system or agent prompt that Sonder sends to a model is a Markdown file
in this directory. These files are the shipped defaults and the source of
truth in git. To change a prompt on a running Sonder, you don't edit these
files. Put a copy in your override directory instead. Sonder reads it again on
the next turn, so there's nothing to restart and no code to change.

| File | Used for |
|---|---|
| `personas/*.md` | Persona presets (`/persona`, the `persona` request field) |
| `trace_instructions.md` | Appended to the system prompt when `/trace` asks for reasoning |
| `runtime_identity.md` | Facts block naming the model that serves the request |
| `agent_local.md`, `agent_hosted.md` | Default system prompt of local and hosted tool-using agents |
| `web_research_agent.md` | Web-routed research runs |
| `claim_reviewer.md` | Reviewer that checks "X does not exist" claims |
| `execution_router_system.md`, `execution_router.md` | Workbench vs Autopilot routing |
| `autopilot_system.md`, `autopilot_planner.md`, `autopilot_reviewer.md`, `autopilot_worker.md` | Autopilot model calls |
| `selfmod_editor.md` | Task given to the self-improvement editing agent |
| `child_lane.md` | Scoped child agent lanes |
| `reflection_*.md` | Lesson and pitfall distillation |
| `grounded_extraction_system.md` | Quote-grounded fact extraction |
| `curriculum_task_generator.md` | Self-curriculum practice-task generator |
| `ollama_alias_system.md` | `SYSTEM` text baked into the `sonder` Ollama alias. It applies only when the alias is rebuilt (`setup_alias.py`) |

`/prompts list` shows every prompt with the version in use: `default`, or
`override@<hash>` when an override is active.

## Overriding a prompt

1. Run `/prompts path <name>` to see where the override file goes. That's
   `$SONDER_PROMPTS_DIR/<name>.md` when the variable is set, otherwise
   `<Sonder state home>/prompts/<name>.md`. Persona files keep their
   `personas/` subdirectory.
2. Copy the default from this directory to that path and edit it.
3. The next turn uses it. To see the text in effect, run `/prompts show <name>`.

To go back to the default, delete the override file.

## Templates

Some prompts contain values that Sonder fills in at runtime. They use
`$name` placeholders, the Python `string.Template` syntax. `/prompts show`
and `/prompts path` list the placeholders for each prompt. An override must
contain exactly the same placeholders, no more and no fewer. For a literal
dollar sign in a template, write `$$`. Braces and `%` are ordinary text.
Prompts that have no placeholders are used exactly as written.

## When an override is ignored

A bad edit never breaks a chat. Sonder ignores an override and uses the
shipped default if the override:

- is empty,
- is larger than 64 KiB,
- isn't valid UTF-8,
- can't be read,
- resolves outside its override directory (for example, through a symlink), or
- doesn't use exactly the prompt's placeholders.

When that happens, Sonder logs one warning for each state of the file, and
`/prompts list` shows the reason next to the prompt.

Sonder normalizes line endings to `\n` and drops one trailing newline at end
of file. If a prompt really does end with a newline, its file ends with a
blank line. Sonder notices a change when the file's modification time or size
changes. `/prompts reload` makes it read every file again.

## If a shipped default is missing

If a file in this directory is missing, the prompt fails with an error rather
than falling back to a copy embedded in code. A copy like that would drift
from the file operators are told to edit. The tests
(`tests/test_prompt_store*.py`) and the packaging allowlist
(`scripts/package_local_system.py`) keep every default present.

There is one exception, `child_lane.md`. The application-layer service keeps
a copy of it for hosts that construct the service without the prompt store,
and a test keeps that copy identical to the file.

`tests/fixtures/prompt_golden.json` records the exact text of each prompt
from before the prompts moved here. The golden test renders every default
against that record. If you change a default on purpose, update the fixture
in the same commit.

Each chat turn records which version of each prompt it used, in the form
`{name: "default" | "override@<hash8>"}`. You can see it in `turn_inspect`
and in `/trace` output.
