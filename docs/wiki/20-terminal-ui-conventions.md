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

- All chrome is built by `sonder_runtime/interfaces/repl/style.py`, a
  stdlib-only module that returns strings and never writes to a stream. The
  REPL paints through `_paint(text, *roles)`, which is `style.s`. Colour
  roles are semantic (`muted`, `accent`, `info`, `success`, `warning`,
  `danger`, `strong`), and glyphs are tokens with an ASCII fallback
  (`style.g`).
- Capabilities are decided once at start (`style.detect_caps`). Colour is
  off with `NO_COLOR`, on `TERM=dumb`, when stdout is not a terminal, and on
  a Windows console that cannot switch to VT mode. Otherwise the level is
  truecolor (`COLORTERM=truecolor|24bit`, or Windows Terminal), 256
  (`TERM=*256color`) or 16. With colour off no escape byte is written; with
  ASCII glyphs the chrome is ASCII.
- The palette is one accent and a few tones, not a rainbow. The accent is
  Iris, the blue-violet of the app (`accentText` in `app/lib/theme.dart`):
  the header mark, the `❯` prompt, the speaker label, headings and list
  markers. `info` names identity (tier, persona) and code; `success`,
  `warning` and `danger` are status; `muted` is the grey the chrome is drawn
  in. The truecolor values are the app's text tones for the same roles, so a
  turn reads the same in both, and every value reaches 4.5:1 on its
  background (`tests/repl/test_style.py` computes it).
- The exact palette needs a known background: it is used when the terminal
  reports 256 colours or truecolor **and** its theme is known
  (`SONDER_THEME=dark|light`, or `COLORFGBG`). Otherwise the roles map to the
  terminal's own 16 colours (the accent is its bright blue), which the
  terminal's scheme keeps readable on either background. `/about` names
  what was detected and the switch.
- Meaning is never carried by colour alone: every status has its glyph and
  its word (`✓ ok`, `✗ failed`, `⊘ refused`).
- The startup header is three lines, not a box (`style.banner`): the mark,
  name and version with the persona, model and tier, mode and endpoint; the
  working directory (`cwd`, the home folder shown as `~`); and the key
  hints. A narrow terminal drops fields in a fixed order instead of
  wrapping. Warnings (elevation, an update, startup notices) follow as lines
  of their own.
- A turn (`_print_chat_result`) is an empty line, the speaker label
  (`◈ Sonder`, or `✗ error`), the tool calls, the answer, and one muted
  footer that is the only place the turn's clock and counts appear. Each
  tool call is two lines (`style.tool_call`): `▸ Read File src/a.py`, and
  under it the result, `└ ✓ ok · 12ms` or `└ ✗ failed · 1.8s`. A failed
  call is always shown, including when the whole turn failed.
- The answer's Markdown is drawn when colour is on (`style.markdown`).
  Headings, list markers, quote bars, rules, inline code and bold are
  styled, and the markers that only ask for a style (`#`, `**`, backticks,
  the fence) are not printed. A fenced block sits between two rules, the
  first naming its language. Code, pipe tables and indented lines are never
  recoloured or rewrapped. With colour off the answer is the source text,
  wrapped.
- While a turn runs, one live line is redrawn in place and erased when the
  turn ends (`_WorkingIndicator`, `style.live_line`). With motion off it
  prints one line, then a progress line every 15 s, with no carriage
  returns and no escapes.
- Where the raw composer cannot frame the prompt (Linux, macOS, a dumb
  `TERM`), the session state is printed as one muted status line
  (`style.status_line`) and the prompt itself is the gutter glyph
  (`_prompt_glyph()`, `❯`), so typed input lines up with the transcript.
  The raw composer (Windows) keeps the status in the title of its rounded
  frame. The frame and the command palette are quiet and the prompt mark
  and the picked palette row take the accent (`_composer_styles()`); the
  text being typed is never tinted, and the tints change no cell of the
  frame.
- Width math counts terminal cells and skips escapes (`style.cell_width`,
  `style.wrap`, `style.truncate`), never `len()`.
- OSC 8 hyperlinks (`style.link`) are emitted only on terminals known to
  support them, so copied or piped output keeps the literal URL.
- Presentation failures never kill the REPL: slash-menu errors fall back
  to `input()`, and a closed stdout makes `/clear` a no-op.

Formatters live in `sonder_runtime/adapters/observability/*_formatting.py`
(and `domain/*_formatting.py`) and are pure plain text; ANSI is applied
only at the REPL layer. New presentation logic should follow that split
and come with exact-string contract tests (see `tests/repl/test_style.py`
and `tests/test_repl_*.py`).

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

- Host refusals and model-transport failures render under the `✗ error`
  label in the danger tone (`_is_repl_error()` decides; a durable
  interaction footer proves a real model answer and is never reclassified).
- Known failure shapes get one muted `hint:` in the footer of the
  interactive turn, or on lines of its own under it when the row has no
  room — `adapters/observability/error_hint_formatting.py` maps grounded
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

- `/help <command>` shows a `policy:` line when a standing permission
  rule refuses a tool the command reaches. For example, `/delete` is a
  hard-coded dry run and is graded `risk: safe`, but the shipped
  `file_delete` deny rule refuses it in every mode, and help now says so.
  The rule is not relaxed for the dry run.

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
- `/work <task>` and `/agent <task>` run managed work only inside the
  selected workspace, like a natural-language work request. The memory
  project name is never used as a directory.
- With no workspace selected, a work request gets a default folder,
  `<YYYY-MM-DD>-<a few words of the task>-<4 hex>`, in the app-owned
  default workspace root. Like Codex's and Claude's managed workspaces it
  is per user and visible, and it does not depend on what this machine
  lists in `[state].workspace_roots`: `%USERPROFILE%\Sonder\workspaces` on
  Windows, `~/Sonder/workspaces` elsewhere, or `[state].default_workspace_root`
  (env `SONDER_DEFAULT_WORKSPACE_ROOT`). The folder is created only if the
  `/workspace-create` permission gate allows it (manual mode with an
  operator asks and shows the path), then selected and announced on one
  line, and the task runs there:

  ```text
  workspace: %USERPROFILE%\Sonder\workspaces\2026-10-01-cool-webpage-3f9a (created because none was selected — /workspace <path> to use another folder)
  ```

  Follow-up work in the same session lands in the same folder; a new
  session gets a new folder. App and autopilot runs without a project use
  the same root, so all creations are in one place.
- Managed console work grants that root by design, without it being listed
  in `[state].workspace_roots`, and only that one tree. Sonder creates the
  root itself, and it is granted only while it exists, is an absolute
  folder that is not a link or junction, is outside every Sonder source
  checkout, and overlaps no private control state (the state home, its
  stores, the running checkout). If any check fails it is simply not
  granted: the console asks instead and configured roots keep working.
- The console asks for a folder instead, holding the task until
  `/workspace` or `/workspace-create` selects one, when
  `SONDER_AUTO_WORKSPACE=0` (or `[state].auto_workspace = false`), after
  `/workspace clear`, when the gate refuses (plan mode, or manual mode
  with nobody to approve), and when the default root fails a check above
  (or no user home is known). Only the last case adds a line first:
  `(no default folder: <reason>)`.
- File commands (`/read`, `/write`, ...) still need the workspace inside
  Sonder's file roots (`SONDER_FILE_ROOTS` or the roots file); the grant
  above covers managed work, not the global file roots.
- When managed work raises a `PermissionError` or `ValueError`, the
  console prints `ERROR: work refused: <reason>` and keeps running. The
  reason is the exception's own message. Managed work uses these types for
  its refusals, both before it starts (no selected project, an incomplete
  workspace inventory or provenance, a malformed recovery identity) and
  during the run (a grant, selection or inventory that changed under it).
  The catch covers the whole managed call, though, so any exception of
  those two types escaping the run is reported the same way. That includes
  an `OSError` with `EACCES` and a `JSONDecodeError`, which may be faults
  rather than refusals. Exceptions of any other type are not caught here.
- A configured workspace root that overlaps private control state (for
  example the state home itself, or the source checkout the console runs
  from) refuses every managed request. The refusal names that root, the
  private path it overlaps, and the `sonder.toml` to remove it from:
  `configured workspace root <root> overlaps Sonder's private control state at <path>; remove it from [state].workspace_roots in <sonder.toml> (or from SONDER_FILE_ROOTS if it is set there)`.

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
