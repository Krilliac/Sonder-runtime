import 'package:flutter/material.dart';

import '../theme.dart';

/// The status kinds chat renders, with the REPL's glyphs and words
/// (APP-PLAN §2.1). Lane B's `lib/ui/status_vocab.dart` is the shared table;
/// this mirrors the rows chat uses until it lands.
enum ChatStatusKind {
  ok('✓', 'done'),
  fail('✗', 'error'),
  refused('⊘', 'refused'),
  warn('!', 'warn'),
  ask('?', 'approve?'),
  skipped('–', 'skipped'),
  note('·', 'note'),
  running('◈', 'working');

  final String glyph;
  final String word;
  const ChatStatusKind(this.glyph, this.word);

  Color color(SonderTokens t) => switch (this) {
        ChatStatusKind.ok => t.ok,
        ChatStatusKind.fail => t.danger,
        ChatStatusKind.refused => t.danger,
        ChatStatusKind.warn => t.warn,
        ChatStatusKind.ask => t.warn,
        ChatStatusKind.skipped => t.muted,
        ChatStatusKind.note => t.muted,
        ChatStatusKind.running => t.accent,
      };
}

/// A notice in the transcript's reading column:
/// `⊘ refused  /write notes.txt`, then detail, then `hint:`, then actions.
/// The word always precedes the title, and the semantics label starts with
/// it, so status never rides on colour alone.
class ChatNotice extends StatelessWidget {
  final ChatStatusKind kind;
  final String? word;
  final String title;
  final String detail;
  final String hint;
  final List<Widget> actions;
  final bool liveRegion;

  const ChatNotice({
    super.key,
    required this.kind,
    this.word,
    required this.title,
    this.detail = '',
    this.hint = '',
    this.actions = const [],
    this.liveRegion = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final tone = kind.color(tokens);
    final label = word ?? kind.word;
    final strong =
        kind == ChatStatusKind.refused || kind == ChatStatusKind.fail;
    return Semantics(
      container: true,
      liveRegion: liveRegion,
      label: '$label: $title',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ExcludeSemantics(
            child: Wrap(
              crossAxisAlignment: WrapCrossAlignment.center,
              spacing: 10,
              runSpacing: 2,
              children: [
                Text(
                  '${kind.glyph} $label',
                  style: tokens.mono(12,
                      color: tone,
                      weight: strong ? FontWeight.w600 : FontWeight.w500),
                ),
                if (title.isNotEmpty)
                  Text(title, style: tokens.mono(12, color: tokens.text)),
              ],
            ),
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            SelectableText(detail,
                style: text.bodyMedium?.copyWith(color: tokens.text2)),
          ],
          if (hint.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text.rich(
              TextSpan(children: [
                TextSpan(
                    text: 'hint: ',
                    style: tokens.mono(12, color: tokens.muted)),
                TextSpan(text: hint),
              ]),
              style: text.bodySmall?.copyWith(color: tokens.text2),
            ),
          ],
          if (actions.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(spacing: 8, runSpacing: 8, children: actions),
          ],
        ],
      ),
    );
  }
}
