# Sonder app design

## North star
Quiet instrument: the conversation is the object. Chrome uses hairlines, restrained teal and a glyph gutter. Product UI for people working with local agents; preserve scanability during long conversations. Avoid decorative dashboards and oversized marketing typography.

## Runtime ownership
`lib/theme.dart` is canonical for tokens, Material component themes, typography, shape and dark/light variants. This document describes that implementation; it does not generate tokens.

## Palette and typography
Signal #63D6C8; dark canvas #0B1117; dark panel #0F171E; dark border #1F2C36. Light canvas #F4F7F8 and light border #DCE5E8. Consume semantic SonderTokens and Material color roles rather than copying values into screens. IBM Plex Sans owns controls/headings; IBM Plex Mono owns transcripts/code/status.

## Layout and interaction
Existing chat uses a 272px rail on desktop, a constrained readable transcript and bottom composer. Agent conversations retain that language with a parent/child list. Narrow layouts show list and transcript separately. Material controls own keyboard focus, tooltips and dialogs. Status always has text, not color alone.

## Motion and content
Use existing Material motion only; no decorative animation. English labels name user actions directly. Pending messages and requested interruption are visible without implying worker acknowledgement. Server transcripts remain available after completion and reconnection.
