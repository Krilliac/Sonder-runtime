# Terminal UI & Observability Conventions

How the interactive REPL presents itself, what piped/scripted callers may
rely on, and where the presentation seams live. This page documents rules
that previously existed only in `sonder_repl.py` docstrings; the code is
the authority, and every rule here names the code that enforces it.

## The two tty questions

The console asks two independent questions, and conflating them has caused
real bugs (a piped script's next line being consumed as a permission
answer):

| Question | Function | Meaning |
|---|---|---|
| stdin is a tty | `_console_has_operator()` | A person is present to answer a permission prompt. Piped stdin means nobody is asked, and a piped console is answered exactly like any other unattended caller: file changes, host programs, and destructive tools are refused with the remedies named, while ask-class tools proceed and are recorded. |
| stdout is a tty | `_stdout_is_interactive()` | Terminal chrome (panels, colors, hints, spinner) may be drawn. `sonder > out.txt` with a human at the keyboard still prompts, but the redirected output stays plain. |

## Color and layout

- All styling goes through one helper, `_paint(text, *styles)`, using the
  palette in `class _Ansi`. Color is enabled only when stdout is a tty and
  `NO_COLOR` is unset; truecolor needs `COLORTERM=truecolor|24bit`, and
  every token has a hand-picked 256-colour cell (`_fg()`) so a plain xterm
  sees the same hierarchy.
- The palette is one accent and a few tones, not a rainbow: `teal` is the
  accent (the header mark, the `❯` prompt, the answer label); `cyan` names
  identity (model, persona) and the `manual` mode; `green`, `amber`, `red`
  and `violet` are ok/plan, warn/acceptEdits, error/danger and auto;
  `text2` and `muted` are the greys the chrome is drawn in. Meaning is never
  carried by colour alone -- every coloured value has its label.
- The launch header is a few packed lines, not a box: `_header_lines()`
  packs `label value` segments to the terminal width (`_terminal_columns()`,
  60..120) by printed width, then the hint and a rule. Width math must use
  `_visible_len()` (escape-aware), never `len()`.
- Where the raw composer cannot frame the prompt (Linux, macOS, a dumb
  `TERM`), the composer title is printed as one muted status line
  (`_status_line()`) and the prompt itself is the gutter glyph
  (`_prompt_glyph()`, `❯`), so typed input lines up with the transcript.
  The raw composer (Windows) keeps the title in its frame.
- Box and gutter glyphs degrade to ASCII when the console encoding cannot
  encode them (`_box_chars()`); a decorative header must never crash a
  launch.
- OSC-8 hyperlinks (`_terminal_link()`) are emitted only when color is
  enabled, so copied or piped output keeps the literal URL.
- Presentation failures never kill the REPL: slash-menu errors fall back
  to `input()`, and a closed stdout makes `/clear` a no-op.

Formatters live in `sonder_runtime/adapters/observability/*_formatting.py`
(and `domain/*_formatting.py`) and are pure plain text; ANSI is applied
only at the REPL layer. New presentation logic should follow that split
and come with exact-string contract tests (see `tests/test_repl_*.py`).

## The scripted output contract

Piped use (`sonder < script.txt`, `echo /stats | sonder`) prints the plain
answer followed by a `[Sonder completed in …]` line, with no chrome. That
shape is a contract pinned by tests and must not change.

`SONDER_REPL_NDJSON=1` opts a **piped** session into one JSON line per
completed chat turn instead — schema `sonder.repl-turn.v1`, owned by
`adapters/observability/repl_machine_output.py`:

```json
{"answer":"…","elapsed_ms":842,"error":false,"feedback_offered":true,
 "hint":"","interaction_id":"…","label":"Sonder","schema":"sonder.repl-turn.v1"}
```

Lines are single-line, sorted-key, ASCII-safe JSON. The schema is
versioned and additive-only. Interactive terminals ignore the flag.

## Error presentation

- Host refusals and model-transport failures render in the red-toned
  `Sonder · error` panel (`_is_repl_error()` decides; a durable
  interaction footer proves a real model answer and is never reclassified).
- Known failure shapes get one muted `hint:` line under the interactive
  panel — `adapters/observability/error_hint_formatting.py` maps grounded
  message literals (Ollama unreachable, HTTP 404/transient rejections,
  model-pin refusals, cloud-disabled, plan-mode refusals) to a single next
  step. Unknown errors get no hint; piped output never includes hints as
  text, though the NDJSON payload carries the same value in `hint`.
- Tests re-assert each trigger literal against the emitting module, so a
  reworded error fails the hint's test rather than silently orphaning it.

## Thread history affordances

- `/sessions` lists past threads: id first (what `/resume` and `/replay`
  accept), turn count, relative age, title, and project
  (`session_list_formatting.py`).
- `/replay [id|title] [N]` re-renders up to N stored turns of a thread
  read-only (`session_replay_formatting.py`): durable footers, trace
  blocks, and activity blocks are stripped, fields are bounded, and the
  current session never changes. `/resume` remains the only way to move
  where the next typed turn lands.
- Raw composer history (Up/Down, Ctrl+R) is process-local and never
  persisted; credential-bearing lines are excluded (`_history_safe()`).

## Usage errors before the permission gate

- A line a command can only answer with its usage text (`/register` with
  no password, `/todo bogus`, `/fact forget` without `<id> confirm`,
  `/mcp bogus`, `/run abc`, a bare `/read`) prints that usage without
  reaching the permission gate, so nobody is asked to approve, or is
  refused, a command that would not have run anything
  (`_branch_usage_error()` in `interfaces/repl/repl.py`). Well-formed
  lines are gated exactly as before.
- Catalogued `/tool` lines reject positional words beyond what the
  command's parameters take (`/status detail` answers
  `/status: unexpected argument 'detail'. usage: /status`). The excess
  words used to be dropped silently. A single free-text parameter still
  takes the whole remainder.

## Workspace scope for file commands

- With a `/workspace` selected, `/files`, `/read`, `/write`, `/append`,
  `/edit`, `/mkdir`, and `/delete` resolve relative paths against that
  directory instead of the process cwd, and refuse any path whose
  canonical form (symlinks followed) leaves it. The file layer also caps
  its roots at the workspace for the command
  (`file_ops.managed_root_scope`), so a path swapped for a link between
  the two checks still cannot escape.
- Selecting a workspace never grants file authority. A workspace outside
  Sonder's file roots (`SONDER_FILE_ROOTS` or the roots file) is refused
  for file commands with a message naming both. `/workspace clear`
  returns the commands to the default roots.

## Interrupting a turn

- Ctrl-C while a turn runs cancels that turn and returns to the prompt;
  the session keeps going. Each turn runs in its own scope of the
  foreground cancellation tree
  (`sonder_runtime/application/foreground_turns.py`); the SIGINT handler
  cancels that scope before unwinding, so model requests and agent steps
  for the turn are refused from then on even if a layer swallowed the
  interrupt. Detached background work (autopilot runs, fleets) is not in
  the turn's scope and keeps running; use `/autopilot cancel` or
  `/agentcancel` for it.
- A cancelled turn clears the per-turn handles: feedback, `/run`, and the
  latest-answer views no longer point at the previous answer. The
  activity feed records the turn as `cancelled`.
- Ctrl-C (or Ctrl-D, or `/exit`) at the idle prompt ends the session.

## Observability surfaces (read-only)

- `activity_tracker` (adapters/observability) is the response/tool
  evidence ledger behind `/activity`, `/report`, and the response footer.
- `LocalObservabilitySink` keeps bounded, redacted process-local events;
  `trace_projection.py` maps them to OTel-shaped spans
  (`sonder.trace-span.v1`) served at `GET /v1/observability/trace`.
  There is no exporter, network path, or persistence (ADR-009).
- `python -m sonder_runtime doctor|status|diagnostics --json` is the
  machine-readable diagnostics path; `_emit()` in `__main__.py` is the
  single JSON/text rendering seam for those commands.
