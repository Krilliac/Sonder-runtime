# ADR 2026-09-27: System and agent prompts as editable Markdown files

**Status:** Accepted
**Date:** 2026-09-27
**Context:** Owner request to edit prompts without changing code, the same
way `system_profile.md` already works. The code is in
`sonder_runtime/adapters/prompt_store.py`,
`sonder_runtime/domain/prompt_templates.py`,
`sonder_runtime/application/ports/prompts.py`, and `prompts/`.

## Decision

Every hard-coded system or agent prompt moves to `prompts/<name>.md`, with
persona files under `prompts/personas/`. Those files ship in git and are the
defaults. `prompt_store.render(name, **fields)` returns the text of an
override if there is one, otherwise the default. Sonder looks for overrides
in `$SONDER_PROMPTS_DIR` first and then in `<state home>/prompts/`. Each call
reads the file again, and a stat check decides whether the cached bytes are
still current. A thread-local turn scope pins the text for one turn. The turn
scope is entered by `_stable_system_context` and by the two chat entry
points.

- **Templates.** Prompts that take runtime values use `string.Template`
  `$name` placeholders. The catalog declares each prompt's fields, and an
  override must use exactly those fields. Removing `$tools` from an agent
  prompt would silently take away context the code depends on, so a
  mismatch counts as an invalid override. The rendered default text is
  byte-identical to the text the code produced before this change. This is
  pinned by `tests/fixtures/prompt_golden.json`, which was captured from the
  previous source.
- **A bad override falls back.** Sonder ignores an override that is empty,
  larger than 64 KiB, not UTF-8, unreadable, escapes its directory through a
  symlink, or has the wrong placeholders. It logs one warning for each state
  of the file and uses the default instead.
- **A missing default is loud.** If a shipped default file is absent, Sonder
  raises `PromptUnavailable` and does not use a copy embedded in code. The
  packaging allowlist lists every default as a required file, and the tests
  check that each one exists. The one embedded copy is `child_lane`, which
  the application-layer lane service uses only when no renderer is injected.
  A test keeps that copy identical to the file.
- **Provenance.** The chat turn trace (`turn_inspect`, `/trace`) records
  `prompts: {name: "default" | "override@<sha256[:8]>"}`. The record is
  bounded by the catalog.
- **`/prompts`** has four actions: `list`, `show`, `path`, and `reload`.
  All of them are read-only. `reload` only clears the cache. On HTTP,
  `/prompts` requires developer authority because it reveals state-home paths
  and operator text.

## Rationale

- Keeping overrides in the state home, as `emotion_vectors` does, means live
  edits never dirty the source checkout or block a guarded `/update`.
- An embedded fallback for every prompt would keep chats working when a file
  is lost in packaging. It would also leave a second copy that nobody edits,
  and that copy would drift. A packaging defect should fail tests, not change
  behaviour silently.
- `string.Template` treats braces and `%` as plain text, so the JSON schema
  examples inside the Autopilot and router prompts need no escaping.

## Consequences

Operators can edit any model-facing instruction and see the change on the
next turn. The runtime identity facts (`runtime_identity_fields`) stay in
code and cannot be edited, but their wording can. `ollama_alias_system.md` is
baked into the Ollama alias, so a change to it applies only when the alias is
rebuilt. Overrides are treated as trusted operator configuration. They carry
the same trust as `system_profile.md` and are not sandboxed.
