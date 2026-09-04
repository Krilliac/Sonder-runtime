# Sonder app design

## North star
Quiet instrument: the conversation is the object. Chrome uses hairlines, restrained teal and a glyph gutter. Product UI for people working with local agents; preserve scanability during long conversations. Avoid decorative dashboards and oversized marketing typography.

## Runtime ownership
`lib/theme.dart` is canonical for tokens, Material component themes, typography, shape and dark/light variants. This document describes that implementation; it does not generate tokens.

## Palette and typography
Signal #63D6C8; dark canvas #0B1117; dark panel #0F171E; dark border #1F2C36. Light canvas #F4F7F8 and light border #DCE5E8. Consume semantic SonderTokens and Material color roles rather than copying values into screens. IBM Plex Sans owns controls, headings and conversation prose; IBM Plex Mono owns code, technical references and status details. `lib/workspace_ui.dart` owns the shared Markdown presentation, 760px reading width, workspace navigation and persistent notices.

## Layout and interaction
Existing chat uses a 272px rail on desktop, a constrained readable transcript and bottom composer. Agent conversations retain that language with a parent/child list. Narrow layouts show list and transcript separately. Material controls own keyboard focus, tooltips and dialogs. Status always has text, not color alone.

Chat, Agents, Runtime and Settings are peer destinations. Chat owns routing and Settings keeps its existing discard guard. Agents use the same shared navigation and guard unsent drafts or uncertain commands before leaving. Parent titles come from loaded conversations; external parents show a short reference and reveal the full selectable/copyable ID on demand. Search and filters describe loaded data, with explicit pagination. Conversation content takes visual priority over collapsed tool details, task metadata and previously read reports.

## Motion and content
Use existing Material motion only; no decorative animation. English labels name user actions directly. Pending messages and requested interruption are visible without implying worker acknowledgement. Server transcripts remain available after completion and reconnection.
