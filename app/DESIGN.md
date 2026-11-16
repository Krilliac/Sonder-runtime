# Sonder app design

## North star
A familiar chat product, quietly Sonder: the conversation is the page. The app reads like the assistants people already use every day: history in a sidebar, a centred reading column, your turns as soft bubbles on the right, answers as plain well-set prose, and one large rounded composer at the bottom. The agent and runtime tooling is one click away in the same shell and never in the way of a conversation.

Hierarchy comes from surface tone and space before borders. One accent, Iris, marks what is live, selected or actionable. Motion is short and answers an action. Every action still answers where it was taken, and status always has a glyph and a word. Nothing borrows another product's mark, name, icon set or signature colour: the mark, the name and the accent (the blue-violet at the middle of the mark's own gradient) are Sonder's. Keep long conversations scannable, and avoid decorative dashboards and oversized marketing type: overview tiles exist to open the page that owns them.

## Runtime ownership
`lib/theme.dart` is canonical for tokens, Material component themes, typography, shape and dark/light variants. This document describes that implementation; it does not generate tokens.

## Palette and typography
Warm-grey neutrals and one accent, in a light and a dark theme that are both first-class. The theme follows the system until the person picks Light or Dark in Settings › Appearance. Screens consume semantic `SonderTokens` and Material colour roles; no screen copies a value.

| Token | Dark | Light | Used for |
|---|---|---|---|
| `canvas` | #1B1A18 | #F7F6F3 | the page behind the conversation |
| `sunken` | #151412 | #F0EEEA | the sidebar; wells (mode chip, code header, table head, detail panels) |
| `panel` | #242321 | #FFFFFF | cards, the composer, menus, dialogs |
| `raised` | #32312D | #E5E2DC | hover and selection pills |
| `bubble` | #2C2B28 | #ECE9E4 | your turn; inline code |
| `hairline` | #34332F | #E0DDD6 | decorative dividers and card edges |
| `hairlineStrong` | #807C74 | #827E77 | control boundaries (field outlines, chip sides, outlined buttons) |
| `text` | #ECEAE4 | #1F1E1C | primary text |
| `text2` | #B8B4AB | #514E49 | secondary text |
| `muted` | #9E9A92 | #66635D | hints, metadata |
| `accent` | #ABA4F7 | #5B54C8 | fills: primary button, switch track, Send |
| `accentText` | #B6B0FA | #4D45B5 | the accent as text, thin glyphs, links and focus rings |
| `onAccent` | #17143A | #FFFFFF | text and icons on an accent fill |
| `ok` | #7FCF96 | #196B3C | status |
| `info` | #74C2E6 | #1B6584 | status |
| `warn` | #EDBF66 | #835500 | status |
| `danger` | #F27F7B | #B52D2D | status |
| `dangerDim` | `danger` at 18% | `danger` at 12% | the wash behind a danger badge and error containers |
| `auto` | #E59AD4 | #913585 | permission tone: auto |
| `mutation` | #EFA070 | #9A4514 | permission tone: file changes |
| `execution` | #CDA88F | #71523D | permission tone: running programs |

The neutrals are warm (red above blue by a few levels) so the page reads as paper, not as a console. Surfaces step in tone, darkest to lightest in both themes: `sunken`, `canvas`, `panel`; `raised` and `bubble` are fills drawn on top of them. Floating surfaces (the composer, menus, the slash palette) carry one soft shadow, `SonderTokens.lift`; toasts and tooltips use the inverse tone. Borders are for control boundaries and code blocks, not for separating regions.

Contrast is a tested contract (`test/theme_contrast_test.dart`): every text and status tone reaches WCAG AA (4.5:1) on all five surfaces, and every control boundary 3:1, in both themes. Measured ratios, on the canvas and at the weakest surface (`raised` in every case):

| Tone | Dark canvas | Dark lowest | Light canvas | Light lowest |
|---|---|---|---|---|
| `text` | 14.46 | 10.82 | 15.41 | 12.88 |
| `text2` | 8.41 | 6.30 | 7.66 | 6.41 |
| `muted` | 6.20 | 4.64 | 5.54 | 4.63 |
| `accentText` | 8.76 | 6.56 | 6.86 | 5.73 |
| `ok` | 9.33 | 6.99 | 6.05 | 5.06 |
| `info` | 8.78 | 6.57 | 5.99 | 5.01 |
| `warn` | 10.14 | 7.59 | 5.95 | 4.98 |
| `danger` | 6.71 | 5.02 | 5.75 | 4.81 |
| `auto` | 8.20 | 6.14 | 6.37 | 5.32 |
| `mutation` | 8.23 | 6.16 | 6.01 | 5.02 |
| `execution` | 7.94 | 5.95 | 6.53 | 5.46 |
| `hairlineStrong` (3:1) | 4.18 | 3.13 | 3.74 | 3.12 |

`onAccent` on `accent` is 7.84 (dark) and 5.92 (light). `accent` is the colour of labelled fills; text, thin glyphs, focus rings and links use `accentText`. The same test pins the accent's hue (blue-violet, 235–265°) and the warm cast of the neutrals, so the palette cannot drift back to teal or to a cold grey unnoticed. The mark keeps its own gradient (`SonderMarkColors`, read only by `lib/ui/sonder_mark.dart`); `theme.dart` is the only file where a colour value is written.

IBM Plex Sans owns controls, headings and conversation prose; IBM Plex Mono owns code and technical values only. Both are bundled; nothing is fetched. Reading text (answers, your turns, the composer) is 16/26; interface text is 14/22; the greeting is 28/36. Labels and headings are sentence case, never all capitals. `lib/workspace_ui.dart` owns the shared Markdown presentation, the 760px reading width, workspace navigation and persistent notices.

Status glyphs Plex lacks (❯ ◈ ⊘ ✗ ▸ ● …) come from `assets/fonts/SonderSymbols.ttf`, a 21-glyph DejaVu Sans Mono subset (Bitstream Vera license, see `assets/fonts/SonderSymbols-LICENSE.txt`). Every Sans and Mono style names it in `fontFamilyFallback`, so an offline install never shows tofu and never fetches a font.

Shape is soft and named for its use (`SonderRadius`): control 8, row 10, menu 12, card 16, sheet 20, bubble 20, composer 26, and `pill` for chips and round buttons.

## Status vocabulary
The app and the REPL speak one status vocabulary and share one palette: the terminal's truecolor roles in style.py are the app's text tones (`muted`, `accentText`, `info`, `ok`, `warn`, `danger`), and `tests/repl/test_style.py` reads `lib/theme.dart` to hold them equal. The source is `sonder_runtime/interfaces/repl/style.py` (`GLYPHS`, `NOTICE_KINDS`, `mode_roles`, `status_line`, `live_line`, `footer`) and `docs/wiki/20-terminal-ui-conventions.md`; the app ports its words, order and glyphs, not its ANSI styling. `lib/ui/status_vocab.dart` holds the table and `test/status_vocab_test.dart` pins it against style.py.

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
- **Wide (≥1000px):** a persistent sidebar (264px, collapsible to a 64px rail with Ctrl/⌘+B) on the `sunken` tone, with no border against the page. Top to bottom: the brand, **New chat**, **Search chats**, the **Chats** row heading the history grouped by date (Today, Yesterday, Previous 7 days, Older), then Agents, Runtime and Settings with their badges (running agents, approvals waiting), and the connection footer. The Chats row is the Chat destination; while the sidebar is expanded the open conversation's own row carries the highlight.
- **Rows are pills:** every sidebar and rail row is a 48px target that draws a 40px `raised` pill when selected or hovered (`HoverSurface`), with a keyboard-only accent ring.
- **Narrow:** the same sidebar is a drawer, and pages show a menu button.
- **Chat stays mounted** (offstage, tickers paused) while another destination is shown, so a streaming turn keeps running. Other destinations build on demand and swap with a short fade-through.
- **Pages read `ShellScope`:** inside the shell they draw no way back to Chat and no workspace menu, because the shell owns navigation. Pumped alone (tests) they keep their own chrome.
- **Leaving a page asks first** when it must. A page wraps itself in `ShellLeaveGuard` (Settings: unsaved changes; Agents: unsent drafts and uncertain commands), and the guard runs for the sidebar, drawer, shortcuts and system back.

**Runtime and Settings are categorized surfaces** (`CategoryScaffold`): a searchable category rail with group headings and badges, and one calm page per category built from `SettingsSection` cards of label, description and control rows. Below 840px the rail becomes a list that opens each page full width.
- **Runtime categories:** Overview (tiles that deep-link), Activity, Models, Memory & learning, Permissions, Server, Observatory, Updates & extensions, Cluster, Developer, About.
- **Settings categories:** General, Connection, Account, Appearance, Privacy, Desktop, Observatory, About.

**Feedback appears where you act.** Every action owns its busy state (`AsyncActionButton`), and its result shows under its own row (`OutcomeView`), so nothing locks a whole page. Confirmations are short toasts; raw command output is `RawOutput` (mono, Copy, collapsed past a few lines), never designed-looking prose.

**Chat:**
- One centred reading column (760px). Your turn is a `bubble` on the right that hugs its text; the answer is unboxed prose from the column's left edge. There is no speaker gutter.
- Markdown is drawn as such: headings, lists, quotes, left-aligned tables with a `sunken` head, inline code on the `bubble` tone.
- What Sonder did before answering (reasoning, tool calls, activity) is one collapsed row above the answer: "Used 3 tools · 2.0s · 1 refused". It opens to the calls.
- Under the answer one footer row holds the actions (Copy / Useful / Edited, and Retry on the newest answer), the receipt's disclosure ("Response details") and the cost line. With a mouse it shows on the newest answer, on hover and on keyboard focus, and stays while its panel is open; on touch it is always shown. Actions confirm visibly. A phone folds every detail into one 48dp disclosure.
- The empty state is the mark, a greeting and a few suggestion chips.
- Code blocks have a language header, Copy and a token-coloured highlighter (`lib/chat/markdown.dart`, `syntax_highlight.dart`).
- Links open through a policy (`lib/chat/links.dart`):
  - web and mail links open externally;
  - a link whose text names another site, or that carries credentials or look-alike characters, shows the real address first;
  - anything else (paths, app schemes) is offered as Copy link, never opened.
- The composer is one large rounded card (`panel`, radius `composer`, the `lift` shadow) pinned under the column. The field grows from one to eight lines. The bar inside the frame holds, in order, the **+** menu, the mode chip, a searchable model picker (routes, local models, Ollama direct), the context ring, the keyboard hint and a round Send/Stop button. The frame's edge darkens while it has focus.
- The **+** menu ("More actions") holds what the bar has no room for: Browse commands, Set project and Server conversations. It opens above its button. The app has no file attach; when it gains one, it belongs in this menu.
- The chat header is the conversation's title with the project as a quiet control beside it.

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

Spacing comes from `SonderSpace`: a 4-point grid (`xxs` 2 to `x6` 64), two half steps for the inside of dense controls (`tight` 6, `snug` 10) and `hair` for a one-pixel optical nudge. No raw number goes into padding, a gap or a spacing argument. Corners come from `SonderRadius` (cards `card`, sheets `sheet`, rows `row`). Cards are `panel` with a `hairline` edge; the category rail is part of the page, and its open category is a `raised` pill. No colour value is written outside `theme.dart`. `test/design_tokens_test.dart` scans the source for both rules.

## Motion and content
Motion is short and purposeful, never decorative.
- **Tokens** (`SonderMotion`): `fast` 150ms for hover and small state flips, `medium` 220ms for panels and category switches, `slow` 320ms for page transitions.
- **Curves:** entrances decelerate (M3 emphasized decelerate), exits accelerate, in-place changes use the standard curve.
- **What animates:**
  - every route uses the shared fade-through (`SonderPageTransitionsBuilder`);
  - new messages fade and rise once and never re-animate;
  - while an answer is being written its text appears as it arrives and the live line's glyph breathes on the one-second tick (no free-running caret);
  - status values cross-fade;
  - meters ease to their value.
- **Reduced motion:** when the platform asks for it, `SonderMotion.of` makes durations zero. Only decoration stops: elapsed timers, live status and progress keep updating.
- **Live progress:** the live line names the real phase (routing, thinking, the tool, model call N, writing). After 20s without output it turns warn with "no output for Ns".

English labels name user actions directly. Pending messages and requested interruption are visible without implying worker acknowledgement. Server transcripts remain available after completion and reconnection. Architecture explanations live once, on Runtime › About, not repeated across pages.

App control is a subordinate Chat toolbar flow. Its selected-conversation strip uses the existing panel and a restrained accent edge; the page keeps the shared reading width and natural narrow scrolling. New conversation fields, paged rows and confirmed revoke dialogs use the existing Material primitives and WorkspaceNotice. A compact Chat title variant preserves its project action when toolbar space is limited. Selection is explicitly separate from managed execution; credentials and technical request payloads never become visual content.
