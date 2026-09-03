# Terminal and app redesign: quiet instrument — 2026-09-03

The maintainer asked for the terminal (REPL/console) and the Flutter app to
look modern and sleek, in the spirit of the current agent-harness apps. The
direction chosen is called **quiet instrument**: the transcript is the
object; chrome is hairlines, one accent and a 24px glyph gutter; dark is the
primary theme and light mirrors it token for token. It was drafted on a
design canvas first (the working files are in
`docs/design/sonder-redesign-2026-09-03/`, one `.dc.html` per artboard plus
`canvas.json`), reviewed once for overflow, contrast and consistency, and
then implemented. Two low-fi alternates (editorial light; dense operator
console) sit beside it on the canvas and were not built.

## Tokens

| role | dark | light |
|---|---|---|
| canvas / panel / raised | `#0B1117` / `#0F171E` / `#141F28` | `#F4F7F8` / `#FFFFFF` / `#EEF3F4` |
| hairline / hairline+ | `#1F2C36` / `#2A3944` | `#DCE5E8` / `#C7D3D8` |
| text / text-2 / muted | `#E7EDF2` / `#A5B2BD` / `#6F7E8A` | `#0F1A21` / `#42525C` / `#5F6F7A` |
| accent (Sonder's signal) / ink on it | `#63D6C8` / `#062A27` | `#1FA597` / `#062A27` |
| ok · plan | `#79D394` | `#2E8B57` |
| info · manual | `#7FB8F0` | `#2F6FB3` |
| warn · ask · acceptEdits | `#F0C36A` | `#946000` |
| danger · dangerous | `#F27B7B` | `#C93C3C` |
| auto | `#D89CF6` | `#7A4BB5` |
| mutation / execution | `#F0A070` / `#C9A58E` | `#B85C2B` / `#7A5A46` |

Type: IBM Plex Sans for UI, IBM Plex Mono for the transcript, code, status
figures and the terminal (tabular numerals where figures line up). Radii by
role: controls 4, rows and code 8, sheets 12, pills. Meaning is never carried
by colour alone: every tone sits beside a label or a glyph, and an unknown
risk or mode stays the neutral outline.

## Terminal (`sonder_runtime/interfaces/repl/repl.py`)

Kept, because tests and scripts depend on them: the `Sonder · answer` /
`Sonder · error` labels, the `╰ 1.20s  (/pass or /fail)` footer, the
`◈ Sonder is working...` line that never claims progress, the byte-exact
piped output (`answer` then `[Sonder completed in …]`), the NDJSON opt-in,
the raw composer's frame on Windows, the ASCII fallback for every glyph, the
stdin/stdout tty split, and the rule that formatters stay plain text with
ANSI applied only at the REPL layer.

Changed:

- `_Ansi` is the token palette: `teal` (accent), `cyan` (identity, manual),
  `green`, `amber`, `red`, `violet` (auto), `text2`, `muted`; 24-bit values
  under `COLORTERM=truecolor|24bit`, hand-picked 256-colour cells otherwise
  (`_fg()`), so a plain xterm sees the same hierarchy.
- The launch header is a few packed lines instead of a box: `_header_lines()`
  packs `label value` segments to the terminal width by printed width
  (`_terminal_columns()`, 60..120), then the hint and a rule. The box helper
  `_banner` is gone with its tests; `_header_lines` is pinned instead (coloured
  segments pack like plain ones; a segment is never split; cp437 fallback).
- Where the raw composer cannot frame the prompt (Linux, macOS, a dumb
  `TERM`), the composer title prints as one muted status line
  (`_status_line()`, the muted tone resuming after each coloured span) and the
  prompt is the gutter glyph `❯` (`_prompt_glyph()`, `>` when the console
  cannot encode it), so typed input lines up with the transcript. Windows
  keeps the framed composer.
- The answer panel's trailing rule is muted; only the label carries the
  accent. Wide composer titles join with `·`; `auto` is violet, not red (red
  is for errors and elevation).

Pinned in `tests/test_repl_input.py` (header packing, ASCII fallback, status
line tone, prompt glyph) with the existing panel, spinner, piped-output and
composer-title pins unchanged; `tests/test_git_tools.py` still finds the
three source labels in the header.

## App (`app/`)

- `theme.dart` is the one place the look lives: `SonderTokens` (a
  `ThemeExtension`, `SonderTokens.of(context)`, `tokens.mono(size)`),
  `SonderRadius`, and `SonderTheme.dark/light` built from the tokens over a
  seeded scheme. Every component theme (app bar, inputs, chips, list tiles,
  rails, dialogs, menus, snack bars, tooltips, switches, buttons, FAB,
  expansion tiles) is set there.
- IBM Plex Sans and Mono are bundled under `app/fonts/` (OFL 1.1, licence
  beside them) and declared in `pubspec.yaml`; a private-first install never
  fetches a font. The hard-coded `'monospace'` / `'Consolas'` families are
  gone; everything monospace goes through `SonderTheme.mono`.
- `safety_colors.dart` draws risk and mode tones from the token set of the
  ambient brightness; unknown values still return the scheme outline.
- Chat: the transcript is a gutter of turns (`_Turn`: ❯ you, ◈ Sonder, ⊘ a
  refusal or failure) in a 760px reading column instead of bubbles; a
  272px rail on wide windows with the mark, chats, projects and the quick
  actions (once: the app bar keeps them on phones); the header is the thread
  title, project chip and turn count in the app bar; the model picker is a
  mono pill; the composer is one panel with the mode chip (an outlined pill
  with the mode's dot), the `/` commands pill, the send button and a
  keyboard hint that yields before it overflows; the status bar is 28px of
  mono figures with the mode on the right. Every test anchor (keys,
  tooltips, semantics labels, the two copy strings) is unchanged.
- Settings: eyebrow group labels over hairlines, the intro as plain text,
  and a three-way theme control (Light / Dark / System). `Settings.themeMode`
  replaces the boolean, migrating `sonder_dark_mode` on first load and still
  writing it for older builds; `darkMode` is derived.
- System: sections are eyebrow-and-hairline breaks in one column instead of
  cards; meters are 4px bars with mono figures; status rows carry a dot and a
  mono value; execution events are hairline rows with the timestamp first.

`flutter analyze` is clean and all 107 app tests pass with the SDK the CI
`analyze` job uses; the Python suite passes with the same one container-
specific failure recorded before.

## What was not done

The Flutter app was restyled, not redesigned in structure: no new screens,
no streaming, no activity drawer (the canvas shows one; the served API has no
stream for it yet). The System screen's twenty panels keep their content and
gained the section style; a panel-by-panel pass is still open. The design
canvas is the reference for both.
