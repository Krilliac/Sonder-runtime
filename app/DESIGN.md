# Sonder app design

## North star
Quiet instrument, alive: the conversation is the object. Chrome uses hairlines, restrained teal and a glyph gutter; motion is short and purposeful; every action answers where it was taken. Product UI for people working with local agents, organized the way the Claude and Codex desktop apps are (one shell, categorized settings); preserve scanability during long conversations. Avoid decorative dashboards and oversized marketing typography: overview tiles exist to open the page that owns them.

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
**One app shell** (`lib/shell/app_shell.dart`) holds the four peer destinations: Chat, Agents, Runtime and Settings, in the manner of the Claude and Codex desktop apps.
- **Wide (≥1000px):** a persistent sidebar (264px, collapsible to a 64px rail with Ctrl/⌘+B) carries the brand, New chat, Search, the destinations with badges (running agents, approvals waiting) and the chat list grouped by date, plus a connection footer.
- **Narrow:** the same sidebar is a drawer, and pages show a menu button.
- **Chat stays mounted** (offstage, tickers paused) while another destination is shown, so a streaming turn keeps running. Other destinations build on demand and swap with a short fade-through.
- **Pages read `ShellScope`:** inside the shell they draw no way back to Chat and no workspace menu, because the shell owns navigation. Pumped alone (tests) they keep their own chrome.
- **Leaving a page asks first** when it must. A page wraps itself in `ShellLeaveGuard` (Settings: unsaved changes; Agents: unsent drafts and uncertain commands), and the guard runs for the sidebar, drawer, shortcuts and system back.

**Runtime and Settings are categorized surfaces** (`CategoryScaffold`): a searchable category rail with group headings and badges, and one calm page per category built from `SettingsSection` cards of label, description and control rows. Below 840px the rail becomes a list that opens each page full width.
- **Runtime categories:** Overview (tiles that deep-link), Activity, Models, Memory & learning, Permissions, Server, Observatory, Updates & extensions, Cluster, Developer, About.
- **Settings categories:** General, Connection, Account, Appearance, Privacy, Desktop, Observatory, About.

**Feedback appears where you act.** Every action owns its busy state (`AsyncActionButton`), and its result shows under its own row (`OutcomeView`), so nothing locks a whole page. Confirmations are short toasts; raw command output is `RawOutput` (mono, Copy, collapsed past a few lines), never designed-looking prose.

**Chat:**
- The transcript keeps the glyph gutter. User turns sit on a faint surface.
- Answers carry Copy / Useful / Edited / Retry, which confirm visibly.
- Code blocks have a language header, Copy and a token-coloured highlighter (`lib/chat/markdown.dart`, `syntax_highlight.dart`).
- Links open through a policy (`lib/chat/links.dart`):
  - web and mail links open externally;
  - a link whose text names another site, or that carries credentials or look-alike characters, shows the real address first;
  - anything else (paths, app schemes) is offered as Copy link, never opened.
- The composer's bottom row is a control strip: mode chip, a searchable model picker (routes, local models, Ollama direct) and a context ring.

**Agents:**
- Rows lead with a status glyph and word, with child lanes threaded under their parent.
- Filters are one segmented control.
- Background work (fleets, autopilot) shows progress.
- Tool calls are collapsible cards with readable arguments, plus Raw JSON and Copy.
- Loading shows skeletons, never a blocking spinner.

Material controls own keyboard focus, tooltips and dialogs. Status always has text, never colour alone. Parent titles come from loaded conversations; external parents show a short reference and reveal the full ID on demand. Search and filters describe loaded data, with explicit pagination.

## Components
`lib/ui/kit.dart` exports the shared kit. Screens compose it rather than hand-rolling cards, rows, badges or busy states:
- **Layout:** `CategoryScaffold`/`SonderCategory`/`CategoryNavigator`.
- **Rows:** `SettingsSection`, `SettingRow` (with `below:` reveal and `modified:` dot), `SwitchRow`, `ValueRow`.
- **Actions and feedback:** `AsyncActionButton` (`confirm:`, `busy:`), `ActionOutcome`/`OutcomeView`, `showSonderToast`, `RawOutput`, `RawDisclosure`, `Disclosure`, `StructuredFields`.
- **Status and metrics:** `StatusPill`, `CountBadge`, `Meter`, `RingMeter`, `StatTile`/`StatGrid` (tiles in a row share its height), `QuietAction`.
- **Loading and motion:** `Skeleton`/`SkeletonRows`/`EmptyState`, `SonderSwitcher`/`SonderReveal`/`HoverSurface`.
- **Shell contract:** `ShellScope`, `ShellLeaveGuard`, `ShellMenuButton`.

Spacing comes from `SonderSpace` (a 4-point grid). Card and sheet corners are `SonderRadius.card`. No hex colours outside `theme.dart`.

## Motion and content
Motion is short and purposeful, never decorative.
- **Tokens** (`SonderMotion`): `fast` 150ms for hover and small state flips, `medium` 220ms for panels and category switches, `slow` 320ms for page transitions.
- **Curves:** entrances decelerate (M3 emphasized decelerate), exits accelerate, in-place changes use the standard curve.
- **What animates:**
  - every route uses the shared fade-through (`SonderPageTransitionsBuilder`);
  - new messages fade and rise once and never re-animate;
  - status values cross-fade;
  - meters ease to their value.
- **Reduced motion:** when the platform asks for it, `SonderMotion.of` makes durations zero. Only decoration stops: elapsed timers, live status and progress keep updating.
- **Live progress:** the live line names the real phase (routing, thinking, the tool, model call N, writing). After 20s without output it turns warn with "no output for Ns".

English labels name user actions directly. Pending messages and requested interruption are visible without implying worker acknowledgement. Server transcripts remain available after completion and reconnection. Architecture explanations live once, on Runtime › About, not repeated across pages.

App control is a subordinate Chat toolbar flow. Its selected-conversation strip uses the existing panel and a restrained teal edge; the page keeps the shared reading width and natural narrow scrolling. New conversation fields, paged rows and confirmed revoke dialogs use the existing Material primitives and WorkspaceNotice. A compact Chat title variant preserves its project action when toolbar space is limited. Selection is explicitly separate from managed execution; credentials and technical request payloads never become visual content.
