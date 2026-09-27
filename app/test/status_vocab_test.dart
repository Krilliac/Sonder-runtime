import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';

/// A copy of the shared values in
/// `sonder_runtime/interfaces/repl/style.py` (`_UNICODE_GLYPHS`,
/// `_ASCII_GLYPHS`, `NOTICE_KINDS`, `mode_roles`). When the repo checkout
/// is present the test also parses style.py itself, so either side changing
/// alone fails here.
const _styleUnicode = {
  'mark': '◈', 'prompt': '❯', 'sep': '·', 'rule': '─', //
  'arrow': '→', 'ellipsis': '…', 'emdash': '—',
  'ok': '✓', 'fail': '✗', 'refused': '⊘', 'warn': '!',
  'ask': '?', 'tool': '▸', 'skip': '–', 'note': '·',
  'up': '↑',
};
const _styleAscii = {
  'mark': '#', 'prompt': '>', 'sep': '|', 'rule': '-', 'arrow': '->', //
  'ellipsis': '...', 'emdash': '--', 'ok': '+', 'fail': 'x',
  'refused': 'x', 'warn': '!', 'ask': '?', 'tool': '-', 'skip': '-',
  'note': '*', 'up': 'Up',
};
// kind: (glyph token, role, word)
const _styleNoticeKinds = {
  'error': ('fail', 'danger', 'error'),
  'refused': ('refused', 'danger', 'refused'),
  'warn': ('warn', 'warning', 'warn'),
  'skipped': ('skip', 'muted', 'skipped'),
  'unknown': ('ask', 'muted', 'unknown'),
  'info': ('note', 'muted', 'note'),
};
const _styleModeRoles = {
  'plan': ['muted'],
  'manual': ['text'],
  'acceptEdits': ['warning'],
  'auto': ['warning', 'strong'],
};

File? _styleFile() {
  for (final path in [
    '../sonder_runtime/interfaces/repl/style.py',
    'sonder_runtime/interfaces/repl/style.py',
  ]) {
    final file = File(path);
    if (file.existsSync()) return file;
  }
  return null;
}

Map<String, String> _parseDict(String source, String name) {
  final start = source.indexOf('$name = {');
  expect(start, greaterThanOrEqualTo(0), reason: '$name not found');
  final end = source.indexOf('}', start);
  final body = source.substring(start, end);
  return {
    for (final m in RegExp(r'"([A-Za-z]+)":\s*"([^"]*)"').allMatches(body))
      m.group(1)!: m.group(2)!,
  };
}

void main() {
  test('the vocabulary table (APP-PLAN §2.1)', () {
    final table = {
      for (final kind in StatusKind.values)
        kind.name: (kind.glyph, kind.word, kind.role.name, kind.strong)
    };
    expect(table, {
      'ok': ('✓', 'ok', 'success', false),
      'fail': ('✗', 'error', 'danger', false),
      'refused': ('⊘', 'refused', 'danger', true),
      'warn': ('!', 'warn', 'warning', false),
      'ask': ('?', 'approve?', 'warning', false),
      'skipped': ('–', 'skipped', 'muted', false),
      'unknown': ('?', 'unknown', 'muted', false),
      'note': ('·', 'note', 'muted', false),
      'running': ('◈', 'working', 'accent', false),
    });
    expect(StatusKind.refused.label, '⊘ refused');
  });

  test('glyphs, notice kinds and mode roles match the copy of style.py', () {
    expect(statusGlyphs, _styleUnicode);
    expect(statusGlyphsAscii, _styleAscii);
    for (final entry in _styleNoticeKinds.entries) {
      final kind = noticeKinds[entry.key]!;
      final (glyph, role, word) = entry.value;
      expect(kind.glyphToken, glyph, reason: entry.key);
      expect(kind.role.name, role, reason: entry.key);
      expect(kind.word, word, reason: entry.key);
    }
    expect(noticeKinds.keys.toSet(), _styleNoticeKinds.keys.toSet());
    for (final entry in _styleModeRoles.entries) {
      expect(modeStyle(entry.key).roles, entry.value, reason: entry.key);
    }
    expect(modeStyle('manual', elevated: true).roles, ['danger', 'reverse']);
    expect(modeStyle('future-mode').roles, ['text']);
  });

  test('the copy still matches style.py in this checkout', () {
    final file = _styleFile();
    if (file == null) {
      markTestSkipped('style.py is not in this checkout');
      return;
    }
    final source = file.readAsStringSync();
    expect(_parseDict(source, '_UNICODE_GLYPHS'), _styleUnicode);
    expect(_parseDict(source, '_ASCII_GLYPHS'), _styleAscii);
    final notices = source.substring(source.indexOf('NOTICE_KINDS = {'));
    for (final entry in _styleNoticeKinds.entries) {
      final (glyph, role, word) = entry.value;
      expect(notices, contains('"${entry.key}": ("$glyph", "$role", "$word")'));
    }
    final roles = source.substring(source.indexOf('def mode_roles'));
    expect(roles, contains('"plan": ("muted",)'));
    expect(roles, contains('"manual": ("text",)'));
    expect(roles, contains('"acceptEdits": ("warning",)'));
    expect(roles, contains('"auto": ("warning", "strong")'));
    expect(roles, contains('return ("danger", "reverse")'));
  });

  test('every kind has a distinct glyph+word and a themed colour', () {
    final labels = StatusKind.values.map((k) => k.label).toSet();
    expect(labels.length, StatusKind.values.length);
    for (final tokens in [SonderTokens.dark, SonderTokens.light]) {
      expect(StatusKind.ok.color(tokens), tokens.ok);
      expect(StatusKind.fail.color(tokens), tokens.danger);
      expect(StatusKind.refused.color(tokens), tokens.danger);
      expect(StatusKind.warn.color(tokens), tokens.warn);
      expect(StatusKind.skipped.color(tokens), tokens.muted);
      expect(StatusKind.running.color(tokens), tokens.accentText);
    }
  });

  test('mode raises need the sheet; lowering does not', () {
    expect(isModeRaise('manual', 'acceptEdits'), isTrue);
    expect(isModeRaise('manual', 'auto'), isTrue);
    expect(isModeRaise('acceptEdits', 'auto'), isTrue);
    expect(isModeRaise('plan', 'auto'), isTrue);
    expect(isModeRaise('unknown-mode', 'acceptEdits'), isTrue);
    expect(isModeRaise('auto', 'plan'), isFalse);
    expect(isModeRaise('auto', 'acceptEdits'), isFalse);
    expect(isModeRaise('acceptEdits', 'manual'), isFalse);
    expect(isModeRaise('plan', 'manual'), isFalse);
    expect(isModeRaise('manual', 'manual'), isFalse);
    expect(isModeRaise('auto', 'auto'), isFalse);
    expect(modeBlurbs.keys, permissionModes);
  });

  test('mode names resolve like permission_modes.resolve_mode', () {
    expect(resolvePermissionMode('AUTO'), 'auto');
    expect(resolvePermissionMode('au'), 'auto');
    expect(resolvePermissionMode('accept-edits'), 'acceptEdits');
    expect(resolvePermissionMode('accept edits'), 'acceptEdits');
    expect(resolvePermissionMode(' Plan '), 'plan');
    expect(resolvePermissionMode('a'), isNull); // ambiguous
    expect(resolvePermissionMode(''), isNull);
    expect(resolvePermissionMode('yolo'), isNull);
    // A spelling the server accepts as a raise must never skip the sheet.
    expect(isModeRaise('manual', 'AUTO'), isTrue);
    expect(isModeRaise('manual', 'au'), isTrue);
    expect(isModeRaise('manual', 'accept-edits'), isTrue);
    expect(isModeRaise('Manual', 'Plan'), isFalse);
    expect(isModeRaise('AUTO', 'acceptEdits'), isFalse);
    // An unresolvable target asks rather than guesses.
    expect(isModeRaise('manual', 'a'), isTrue);
  });
}
