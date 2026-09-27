# Sonder app design

## North star
Quiet instrument: the conversation is the object. Chrome uses hairlines, restrained teal and a glyph gutter. Product UI for people working with local agents; preserve scanability during long conversations. Avoid decorative dashboards and oversized marketing typography.

## Runtime ownership
`lib/theme.dart` is canonical for tokens, Material component themes, typography, shape and dark/light variants. This document describes that implementation; it does not generate tokens.

## Palette and typography
Signal #63D6C8; dark canvas #0B1117; dark panel #0F171E; dark border #1F2C36. Light canvas #F4F7F8 and light border #DCE5E8. Consume semantic SonderTokens and Material color roles rather than copying values into screens. IBM Plex Sans owns controls, headings and conversation prose; IBM Plex Mono owns code, technical references and status details. `lib/workspace_ui.dart` owns the shared Markdown presentation, 760px reading width, workspace navigation and persistent notices.

Status glyphs Plex lacks (❯ ◈ ⊘ ✗ ▸ ● …) come from `assets/fonts/SonderSymbols.ttf`, a 21-glyph DejaVu Sans Mono subset (Bitstream Vera license, see `assets/fonts/SonderSymbols-LICENSE.txt`). Every Sans and Mono style names it in `fontFamilyFallback`, so an offline install never shows tofu and never fetches a font.

Contrast is a tested contract (`test/theme_contrast_test.dart`): every text and status colour reaches 4.5:1 on canvas, panel and raised, and every control boundary 3:1, in both themes. `accent` is the colour of labelled fills and the switch track; text, thin glyphs, focus rings and the selected rail icon use `accentText` (light #0A7B73; the light fill #1FA597 is only 2.8:1 on the canvas). Field outlines, chip sides and outlined buttons use `hairlineStrong` (dark #5A6C7E, light #7A8D98); `hairline` stays for decorative dividers. Light `ok` is #1F7A45, light `danger` #C23636, light `mutation` #A4501F and dark `muted` #7F8E9A.

## Status vocabulary
The app and the REPL speak one status vocabulary. The source is `sonder_runtime/interfaces/repl/style.py` (`GLYPHS`, `NOTICE_KINDS`, `mode_roles`, `status_line`, `live_line`, `footer`) and `docs/wiki/20-terminal-ui-conventions.md`; the app ports its words, order and glyphs, not its ANSI styling. `lib/ui/status_vocab.dart` holds the table and `test/status_vocab_test.dart` pins it against style.py.

| Kind | Glyph | Word | Token | Used for |
|---|---|---|---|---|
| ok | ✓ | done / ok / approved | ok | finished turn, healthy row, approval issued |
| fail | ✗ | error | danger | transport or server failure |
| refused | ⊘ | refused | danger (strong) | a permission gate refused a call |
| warn | ! | warn / needs you | warn | degraded, action needed, pending approval |
| ask | ? | approve? | warn | the approval sheet header |
| skipped | – | skipped / off | muted | off by design, not applicable |
| unknown | ? | unknown | muted | outcome not known (aborted mutation) |
| note | · | note | muted | info |
| running | ◈ | working | accentText | live turn, running work run |

A kind always shows its glyph and word; colour never carries status alone, and the word is what a screen reader hears. Synonyms come only from the same row. Modes follow `mode_roles`: plan muted, manual text, acceptEdits warn, auto warn and semibold, ELEVATED a danger badge. Their one-line effects are "reads only — no changes", "asks before changes", "file edits run without asking" and "edits and programs run without asking".

`lib/ui/status_line.dart` ports the three line formats with style.py's field and drop order, measured in cells: the status strip (`code · sonder:latest · manual · ctx 2.1k/8.2k · 2 agents`; the mode word is never dropped), the live line (`◈ working · routing · 12s · sonder:latest`, with Stop as a button beside it) and the answer footer (`done 61.2s · 2 model calls · 2.6k→143 tok`). `test/status_line_test.dart` asserts exact strings captured from style.py. `lib/ui/status_row.dart` draws a status row (glyph, word, label, value) that stacks the value under the label below 480px.

## Notices & approvals
`WorkspaceNotice(kind:, title:, detail:, hint:, actions:)` is the app's port of the REPL notice: `⊘ refused  /write notes.txt`, then the detail, a muted `hint:` line and the actions, with titles starting in one column. warn uses `warn` (not danger), ok uses `ok`, error and refused use `danger`. Its semantics label starts with the word ("refused: …") and it is a live region. The legacy `message:`/`tone:` form still compiles and maps info → note, success → ok, warning → warn.

`lib/ui/approval_sheet.dart` holds the approval sheet (`? approve  write_file · call 3f9a12c0`, the redacted arguments, when and why it was refused, the one-call explainer, a lifetime picker, Cancel and a warn-toned **Approve once**), its console fallback (`/approve <id>` with Copy when the server has no HTTP approvals) and the post-approval receipt (`✓ approved … once · nonce … · valid 15 minutes` with **Retry the request** and **Revoke**). `lib/ui/raise_mode_sheet.dart` holds the raise sheet (`! raise mode  manual → auto`, the effect for every chat and agent on the host, "Destructive tools still need a person.", Cancel and **Switch to auto**, danger-toned for auto and warn-toned for acceptEdits). Both are presentational: callers perform the request. They open as a bottom sheet below 600px and a dialog above. Copy for these components lives in `lib/ui/strings.dart`.

Wording: name the effect before the mechanism; buttons say what they do ("Approve once", never "OK"); the app renders the server's refusal wording and never adds bypass hints for credential stores.

Goldens for these primitives (`test/goldens/primitives/`, tag `golden`, Linux only) load the bundled Plex, Material Icons and SonderSymbols faces, so they are hermetic. When a golden fails in CI, the `analyze` job of `build-apps.yml` uploads the master, test and diff images from `test/goldens/failures/` as the `flutter-golden-failures` artifact.

## Layout and interaction
Existing chat uses a 272px rail on desktop, a constrained readable transcript and bottom composer. Agent conversations retain that language with a parent/child list. Narrow layouts show list and transcript separately. Material controls own keyboard focus, tooltips and dialogs. Status always has text, not color alone.

Chat, Agents, Runtime and Settings are peer destinations. Chat owns routing and Settings keeps its existing discard guard. Agents use the same shared navigation and guard unsent drafts or uncertain commands before leaving. Parent titles come from loaded conversations; external parents show a short reference and reveal the full selectable/copyable ID on demand. Search and filters describe loaded data, with explicit pagination. Conversation content takes visual priority over collapsed tool details, task metadata and previously read reports.

## Motion and content
Use existing Material motion only; no decorative animation. English labels name user actions directly. Pending messages and requested interruption are visible without implying worker acknowledgement. Server transcripts remain available after completion and reconnection.

App control is a subordinate Chat toolbar flow. Its selected-conversation strip uses the existing panel and a restrained teal edge; the page keeps the shared reading width and natural narrow scrolling. New conversation fields, paged rows and confirmed revoke dialogs use the existing Material primitives and WorkspaceNotice. A compact Chat title variant preserves its project action when toolbar space is limited. Selection is explicitly separate from managed execution; credentials and technical request payloads never become visual content.
