# App design system

How the Flutter app (`app/`) is styled, so a change lands in one place and
every screen follows.

## One file owns the look

`app/lib/theme.dart`:

- `SonderTokens` — the colour tokens of one theme (canvas, panel, raised,
  two hairlines, three text greys, the accent and its ink, six semantic
  tones). It is a `ThemeExtension`; read it with `SonderTokens.of(context)`
  and never paint a literal colour in a screen. `tokens.mono(size)` is the
  monospace style with tabular numerals for figures, paths and code.
- `SonderRadius` — controls 4, rows and code 8, sheets 12, pills.
- `SonderTheme.dark` / `SonderTheme.light` — `ThemeData` built from the tokens
  over a seeded scheme, with every component theme set so a bare `Card`,
  `TextField`, `Chip`, `FilledButton` or `Tooltip` already looks right.
- `SonderTheme.sans` / `SonderTheme.mono` — the bundled IBM Plex faces
  (`app/fonts/`, OFL 1.1). Name them; never a platform family.

`app/lib/safety_colors.dart` maps risk bands and autonomy modes to tones from
the same token set. Unknown values return the scheme outline: unknown policy
must never look like a known safe one. Meaning is never carried by colour
alone — pair every tone with a label or a glyph.

## The transcript

A turn is a glyph in a 24px gutter (❯ you, ◈ Sonder, ⊘ a refusal or failure)
and content in a 760px reading column. Tool activity, reasoning, receipts
and error details are collapsed rows under the answer. Refusals are panels
with the real remedies as buttons. Nothing is a bubble.

## Theme preference

`Settings.themeMode` is `dark` (the primary theme), `light` or `system`;
`darkMode` is derived from it for older call sites, and the old boolean
preference is migrated on first load and still written for older builds.

## Reference

The design canvas the app and terminal were built from lives in
`docs/design/sonder-redesign-2026-09-03/`; the decision record is
`docs/architecture/evidence/REDESIGN-QUIET-INSTRUMENT-2026-09-03.md`. The
terminal's conventions are in `20-terminal-ui-conventions.md`.
