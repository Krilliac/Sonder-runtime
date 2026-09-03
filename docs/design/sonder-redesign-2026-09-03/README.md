# Sonder redesign canvas — quiet instrument (2026-09-03)

The working files of the terminal and app redesign: one artboard per
`.dc.html`, laid out by `canvas.json`. They are static mockups grounded in
the runtime's own vocabulary (real commands, the loopback endpoint, the
permission modes, the pinned terminal strings).

| artboard | shows |
|---|---|
| `Main` | desktop chat, dark: rail, transcript, refusal card, composer, activity drawer, status bar |
| `AppChatLight` | the same screen in the light theme |
| `AppSystem` | the System screen as one instrument panel |
| `PhoneChat`, `PhoneSettings` | the phone layouts |
| `Terminal`, `TerminalGate`, `TerminalWorking` | a REPL session; approvals and modes; the working line and piped output |
| `Tokens` | the colour, type, radius, spacing and glyph tokens |
| `DirectionB`, `DirectionC` | low-fi alternates that were not chosen |

The tokens are implemented in `app/lib/theme.dart` and
`sonder_runtime/interfaces/repl/repl.py` (`_Ansi`); the decision record is
`docs/architecture/evidence/REDESIGN-QUIET-INSTRUMENT-2026-09-03.md`.
