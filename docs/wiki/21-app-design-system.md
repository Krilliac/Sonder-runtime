# App design system

How the Flutter app (`app/`) is styled, so a change lands in one place and
every screen follows.

## One file owns the look

`app/lib/theme.dart`:

- `SonderTokens` — the colour tokens of one theme: five surfaces (sunken,
  canvas, panel, raised, bubble), two hairlines, three text greys, the Iris
  accent with its text tone and its ink, and the status and permission
  tones (ok, info, warn, danger and its wash, auto, mutation, execution).
  It is a `ThemeExtension`; read it with `SonderTokens.of(context)` and
  never paint a literal colour in a screen. `tokens.mono(size)` is the
  monospace style with tabular numerals for figures, paths and code, and
  `tokens.lift` is the one shadow floating surfaces carry.
- `SonderSpace` — the 4-point spacing scale. No one-off spacing literals.
- `SonderRadius` — control 8, row 10, menu 12, card 16, sheet and bubble
  20, composer 26, and `pill`.
- `SonderMotion` — fast 150 ms, medium 220 ms, slow 320 ms; reduced motion
  removes them.
- `SonderTheme.dark` / `SonderTheme.light` — `ThemeData` built from the tokens
  over a seeded scheme, with every component theme set so a bare `Card`,
  `TextField`, `Chip`, `FilledButton` or `Tooltip` already looks right.
- `SonderTheme.sans` / `SonderTheme.mono` — the bundled IBM Plex faces
  (`app/fonts/`, OFL 1.1). Name them; never a platform family.

`app/lib/safety_colors.dart` maps risk bands and autonomy modes to tones from
the same token set. Unknown values return the scheme outline: unknown policy
must never look like a known safe one. Meaning is never carried by colour
alone — pair every tone with a label or a glyph.

## The conversation

One centred 760px reading column. Your turn is a soft bubble on the right
that hugs its text; the answer is unboxed prose from the column's left edge,
with Markdown drawn as such. What Sonder did before answering (reasoning,
tool calls, activity) is one collapsed row above the answer ("Used 3 tools ·
2.0s · 1 refused"). Under the answer one footer row holds the actions, the
receipt's disclosure and the cost line. Refusals are panels with the real
remedies as buttons. The composer is one large rounded card under the
column with the mode chip, the model picker and the + menu inside it.

## The shell

A sidebar in the order people know from other chat apps: New chat, Search
chats, the Chats row heading the history grouped by date, then Agents,
Runtime and Settings, and the connection footer. Rows are pills
(`HoverSurface` in `app/lib/ui/kit/`): a 48px target that draws a 40px
`raised` pill when selected or hovered.

## Theme preference

`Settings.themeMode` is `system` (the default for a new install), `light`
or `dark`; both themes are first-class. `darkMode` is derived from it for
older call sites, and the old boolean preference is migrated on first load
and still written for older builds.

## Reference

`app/DESIGN.md` is the full description of the system: the token table with
both themes' values and measured contrast, the type scale, the shell and
every surface. `app/UX-CONTRACT.md` lists the behaviours the tests hold.
The terminal's conventions are in `20-terminal-ui-conventions.md`; it shares
the app's accent and status vocabulary.

The earlier direction ("quiet instrument", 2026-09-03) is recorded in
`docs/design/sonder-redesign-2026-09-03/` and
`docs/architecture/evidence/REDESIGN-QUIET-INSTRUMENT-2026-09-03.md`.
