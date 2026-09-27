import 'package:flutter/material.dart';

import '../theme.dart';

/// The one status vocabulary the app and the terminal REPL share.
///
/// Source of truth: `sonder_runtime/interfaces/repl/style.py` (`GLYPHS`,
/// `NOTICE_KINDS`, `mode_roles`) and `docs/wiki/20-terminal-ui-conventions.md`.
/// The app ports the REPL's *words, order and glyphs*, never its ANSI
/// styling. Colour never carries status alone: every kind has a glyph and a
/// word, and the word is what screen readers hear.
///
/// `test/status_vocab_test.dart` pins this table against a copy of the
/// style.py values and, when the repo checkout is present, against style.py
/// itself, so a wording change on either side fails a test.

/// A semantic colour role, named as in style.py's `ROLES`.
enum StatusRole { text, muted, accent, success, warning, danger }

/// Every status the app shows. See the table in APP-PLAN §2.1 / DESIGN.md.
enum StatusKind {
  /// Finished turn, healthy row, approval issued.
  ok('ok', 'ok', StatusRole.success),

  /// Transport or server failure.
  fail('fail', 'error', StatusRole.danger),

  /// A permission gate refused a call (danger, strong).
  refused('refused', 'refused', StatusRole.danger, strong: true),

  /// Degraded, action needed, pending approval.
  warn('warn', 'warn', StatusRole.warning),

  /// The approval sheet header.
  ask('ask', 'approve?', StatusRole.warning),

  /// Off by design, not applicable.
  skipped('skip', 'skipped', StatusRole.muted),

  /// Outcome not known (an aborted mutation).
  unknown('ask', 'unknown', StatusRole.muted),

  /// Information.
  note('note', 'note', StatusRole.muted),

  /// A live turn or a running work run.
  running('mark', 'working', StatusRole.accent);

  /// The style.py glyph token name (`g(name)`).
  final String glyphToken;

  /// The default word. Callers may substitute a synonym from the same row of
  /// the table ("done", "approved", "off", "needs you"), never a new meaning.
  final String word;

  /// The colour role.
  final StatusRole role;

  /// Whether the word is drawn in the strong (semibold) weight.
  final bool strong;

  const StatusKind(this.glyphToken, this.word, this.role,
      {this.strong = false});

  /// The Unicode glyph (bundled in SonderSymbols where Plex lacks it).
  String get glyph => statusGlyphs[glyphToken]!;

  /// The ASCII fallback glyph, as the REPL draws it on legacy consoles.
  String get asciiGlyph => statusGlyphsAscii[glyphToken]!;

  /// `✓ ok`, `⊘ refused`: the glyph-and-word label a notice or row leads with.
  String get label => '$glyph $word';

  /// The colour of this kind's glyph and word in [tokens].
  Color color(SonderTokens tokens) => roleColor(tokens, role);
}

/// The REPL's Unicode glyph tokens (style.py `_UNICODE_GLYPHS`).
const statusGlyphs = <String, String>{
  'mark': '◈',
  'prompt': '❯',
  'sep': '·',
  'rule': '─',
  'arrow': '→',
  'ellipsis': '…',
  'emdash': '—',
  'ok': '✓',
  'fail': '✗',
  'refused': '⊘',
  'warn': '!',
  'ask': '?',
  'tool': '▸',
  'skip': '–',
  'note': '·',
  'up': '↑',
};

/// The REPL's ASCII glyph tokens (style.py `_ASCII_GLYPHS`).
const statusGlyphsAscii = <String, String>{
  'mark': '#',
  'prompt': '>',
  'sep': '|',
  'rule': '-',
  'arrow': '->',
  'ellipsis': '...',
  'emdash': '--',
  'ok': '+',
  'fail': 'x',
  'refused': 'x',
  'warn': '!',
  'ask': '?',
  'tool': '-',
  'skip': '-',
  'note': '*',
  'up': 'Up',
};

/// The REPL's notice kinds (style.py `NOTICE_KINDS`), mapped onto
/// [StatusKind]. `info` is the REPL's name for [StatusKind.note].
const noticeKinds = <String, StatusKind>{
  'error': StatusKind.fail,
  'refused': StatusKind.refused,
  'warn': StatusKind.warn,
  'skipped': StatusKind.skipped,
  'unknown': StatusKind.unknown,
  'info': StatusKind.note,
};

/// The colour of a semantic role in [tokens]. Accent text uses
/// [SonderTokens.accentText], which keeps 4.5:1 where the fill accent
/// would not.
Color roleColor(SonderTokens tokens, StatusRole role) => switch (role) {
      StatusRole.text => tokens.text,
      StatusRole.muted => tokens.muted,
      StatusRole.accent => tokens.accentText,
      StatusRole.success => tokens.ok,
      StatusRole.warning => tokens.warn,
      StatusRole.danger => tokens.danger,
    };

// ---------------------------------------------------------------------------
// Permission modes (style.py `mode_roles`)
// ---------------------------------------------------------------------------

/// The known permission modes, least to most autonomous.
const permissionModes = <String>['plan', 'manual', 'acceptEdits', 'auto'];

/// How a mode word is drawn: its role, whether it is strong, and whether it
/// is a reversed badge (only ELEVATED).
@immutable
class ModeStyle {
  final StatusRole role;
  final bool strong;
  final bool reversed;
  const ModeStyle(this.role, {this.strong = false, this.reversed = false});

  /// The style.py role tuple, for tests: `('warning', 'strong')`.
  List<String> get roles => [
        role.name,
        if (strong) 'strong',
        if (reversed) 'reverse',
      ];

  Color color(SonderTokens tokens) => roleColor(tokens, role);

  @override
  bool operator ==(Object other) =>
      other is ModeStyle &&
      other.role == role &&
      other.strong == strong &&
      other.reversed == reversed;

  @override
  int get hashCode => Object.hash(role, strong, reversed);
}

/// The style of a mode word: plan muted, manual text, acceptEdits warn,
/// auto warn+strong, ELEVATED danger reversed. Unknown modes read as text.
ModeStyle modeStyle(String mode, {bool elevated = false}) {
  if (elevated) {
    return const ModeStyle(StatusRole.danger, reversed: true);
  }
  return switch (mode) {
    'plan' => const ModeStyle(StatusRole.muted),
    'manual' => const ModeStyle(StatusRole.text),
    'acceptEdits' => const ModeStyle(StatusRole.warning),
    'auto' => const ModeStyle(StatusRole.warning, strong: true),
    _ => const ModeStyle(StatusRole.text),
  };
}

/// One-line effect of each mode, effect before mechanism.
const modeBlurbs = <String, String>{
  'plan': 'reads only — no changes',
  'manual': 'asks before changes',
  'acceptEdits': 'file edits run without asking',
  'auto': 'edits and programs run without asking',
};

/// The position of [mode] in [permissionModes], or -1 when unknown.
int modeRank(String mode) => permissionModes.indexOf(mode);

/// The mode [name] selects, resolved exactly as the server's
/// `permission_modes.resolve_mode` does: case-insensitive, spaces and dashes
/// ignored, and an unambiguous prefix accepted (`AUTO`, `au`, `accept-edits`
/// all select a mode). Returns null when nothing or more than one mode
/// matches, which the server rejects.
String? resolvePermissionMode(String name) {
  final wanted =
      name.trim().toLowerCase().replaceAll(' ', '').replaceAll('-', '');
  if (wanted.isEmpty) return null;
  for (final mode in permissionModes) {
    if (mode.toLowerCase() == wanted) return mode;
  }
  final hits =
      permissionModes.where((m) => m.toLowerCase().startsWith(wanted)).toList();
  return hits.length == 1 ? hits.single : null;
}

/// Whether switching [from] → [to] raises autonomy and so needs the raise
/// confirmation sheet: any move up to `acceptEdits` or `auto`. Lowering, or
/// moving between plan and manual, needs no sheet.
///
/// Both names are resolved like the server resolves them
/// ([resolvePermissionMode]), so `/mode AUTO` or `/mode au` cannot slip past
/// the sheet as an "unknown" target the server then accepts as `auto`. A
/// target that does not resolve is treated as a raise (ask rather than
/// guess). An unknown current mode is treated as the most restrictive, so a
/// move to acceptEdits/auto from it still asks.
bool isModeRaise(String from, String to) {
  final resolvedTo = resolvePermissionMode(to);
  if (resolvedTo == null) return true;
  final target = modeRank(resolvedTo);
  if (target < modeRank('acceptEdits')) return false;
  final resolvedFrom = resolvePermissionMode(from);
  final current = resolvedFrom == null ? -1 : modeRank(resolvedFrom);
  return current < target;
}
