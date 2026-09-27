import 'package:flutter/material.dart';

import '../theme.dart';
import 'status_vocab.dart';

/// The glyph and word of a [StatusKind], in its colour: `✓ ok`, `⊘ refused`.
///
/// The glyph is decorative for assistive technology; the word is what a
/// screen reader hears, so colour and glyph never carry status alone.
class StatusMark extends StatelessWidget {
  final StatusKind kind;

  /// A synonym from the same row of the vocabulary ("done", "off").
  final String? word;

  /// Mono size of the mark.
  final double size;

  /// A fixed width, so a column of marks lines up (the REPL's column 11).
  final double? width;

  const StatusMark(this.kind,
      {super.key, this.word, this.size = 13, this.width});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final shown = word ?? kind.word;
    final style = tokens.mono(size,
        color: kind.color(tokens),
        weight:
            kind.role == StatusRole.danger ? FontWeight.w600 : FontWeight.w500);
    final mark = Row(mainAxisSize: MainAxisSize.min, children: [
      ExcludeSemantics(child: Text(kind.glyph, style: style)),
      SizedBox(width: size * 0.5),
      Flexible(
        child: Text(shown,
            style: style, maxLines: 1, overflow: TextOverflow.ellipsis),
      ),
    ]);
    return width == null ? mark : SizedBox(width: width, child: mark);
  }
}

/// One status row: `✓ ok   Server   mypc · 127.0.0.1:11435 · up 3h`.
///
/// Wide layouts use three aligned columns (mark, label, value) and an
/// optional trailing action; below [stackBelow] logical pixels the value
/// stacks under the label, so a phone never truncates it. The whole row is
/// one semantics node that reads "ok, Server: mypc …".
class StatusRow extends StatelessWidget {
  final StatusKind kind;
  final String label;
  final String value;

  /// A synonym for the kind's word ("off", "done").
  final String? word;

  /// An optional action at the end of the row, such as `[Review]`.
  final Widget? trailing;

  /// Keeps the values dimmed and says so, after a failed refresh.
  final bool stale;

  /// Width below which the value stacks under the label.
  final double stackBelow;

  const StatusRow({
    super.key,
    required this.kind,
    required this.label,
    required this.value,
    this.word,
    this.trailing,
    this.stale = false,
    this.stackBelow = 480,
  });

  static const markWidth = 104.0;
  static const labelWidth = 128.0;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final valueStyle =
        text.bodyMedium?.copyWith(color: stale ? tokens.muted : tokens.text2);
    final labelStyle = text.bodyMedium?.copyWith(
        color: stale ? tokens.muted : tokens.text, fontWeight: FontWeight.w500);
    final semantics = '${word ?? kind.word}, $label: $value';
    return LayoutBuilder(builder: (context, constraints) {
      final narrow = constraints.maxWidth < stackBelow;
      final Widget content;
      if (narrow) {
        content = Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(children: [
                StatusMark(kind, word: word, width: markWidth),
                Expanded(
                    child: Text(label,
                        style: labelStyle,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis)),
              ]),
              Padding(
                padding: const EdgeInsets.only(left: markWidth, top: 2),
                child: Text(value, style: valueStyle),
              ),
            ]);
      } else {
        content = Row(
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: [
              StatusMark(kind, word: word, width: markWidth),
              SizedBox(
                  width: labelWidth,
                  child: Text(label,
                      style: labelStyle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis)),
              Expanded(child: Text(value, style: valueStyle)),
            ]);
      }
      final trailing = this.trailing;
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(children: [
          Expanded(
              child: Semantics(
                  label: semantics, excludeSemantics: true, child: content)),
          if (trailing != null) ...[const SizedBox(width: 8), trailing],
        ]),
      );
    });
  }
}
