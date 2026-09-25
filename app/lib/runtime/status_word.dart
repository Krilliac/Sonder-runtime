/// The shared status vocabulary as the Runtime screen uses it.
///
/// Values mirror `sonder_runtime/interfaces/repl/style.py` (`_UNICODE_GLYPHS`
/// and `NOTICE_KINDS`) and plan §2.1. Lane B owns the app-wide copy in
/// `lib/ui/status_vocab.dart`; this file is the Runtime lane's stand-in until
/// that lands, and the merge replaces [RuntimeStatus] with lane B's
/// `StatusKind` (same members, same glyphs and words).
library;

import 'package:flutter/material.dart';

import '../theme.dart';

enum RuntimeStatus {
  ok('✓', 'ok'),
  fail('✗', 'error'),
  refused('⊘', 'refused'),
  warn('!', 'warn'),
  ask('?', 'approve?'),
  skipped('–', 'off'),
  unknown('?', 'unknown'),
  note('·', 'note'),
  running('◈', 'working');

  final String glyph;
  final String word;
  const RuntimeStatus(this.glyph, this.word);

  /// The bundled Plex faces lack ✗ ⊘ ◈. Until lane B's SonderSymbols
  /// fallback lands, those three draw as icons of the same shape so nothing
  /// renders as tofu offline (plan P2-3).
  IconData? get fallbackIcon => switch (this) {
        RuntimeStatus.fail => Icons.close,
        RuntimeStatus.refused => Icons.block,
        RuntimeStatus.running => Icons.diamond_outlined,
        _ => null,
      };

  /// Colour never carries status alone: every use also shows [word].
  Color color(SonderTokens tokens) => switch (this) {
        RuntimeStatus.ok => tokens.ok,
        RuntimeStatus.fail || RuntimeStatus.refused => tokens.danger,
        RuntimeStatus.warn || RuntimeStatus.ask => tokens.warn,
        RuntimeStatus.running => tokens.accent,
        _ => tokens.muted,
      };

  /// A problem the operator should look at (counts toward attention).
  bool get isProblem => const {
        RuntimeStatus.fail,
        RuntimeStatus.refused,
        RuntimeStatus.warn,
        RuntimeStatus.ask,
      }.contains(this);
}

/// `✓ ok` as one fixed-width, labelled cell: glyph and word, never colour
/// alone. Screen readers hear the word, not the glyph.
class RuntimeStatusWord extends StatelessWidget {
  final RuntimeStatus status;
  final String? word;
  final double width;

  const RuntimeStatusWord(this.status, {super.key, this.word, this.width = 92});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final color = status.color(tokens);
    final label = word ?? status.word;
    return Semantics(
      label: label,
      excludeSemantics: true,
      child: SizedBox(
        width: width,
        child: Text.rich(
          TextSpan(children: [
            if (status.fallbackIcon != null)
              WidgetSpan(
                  alignment: PlaceholderAlignment.middle,
                  child: Icon(status.fallbackIcon, size: 13, color: color))
            else
              TextSpan(
                  text: status.glyph,
                  style:
                      tokens.mono(13, color: color, weight: FontWeight.w600)),
            const TextSpan(text: ' '),
            TextSpan(
                text: label,
                style: tokens.mono(12,
                    color: color,
                    weight:
                        status.isProblem ? FontWeight.w600 : FontWeight.w500)),
          ]),
          maxLines: 1,
          overflow: TextOverflow.clip,
        ),
      ),
    );
  }
}
