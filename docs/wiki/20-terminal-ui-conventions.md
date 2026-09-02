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
  `NO_COLOR` is unset; truecolor needs `COLORTERM=truecolor|24bit`.
- Width math must use `_visible_len()` (escape-aware), never `len()`.
- Box glyphs degrade to ASCII when the console encoding cannot encode
  them (`_box_chars()`); a decorative header must never crash a launch.
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
