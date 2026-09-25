import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

import '../theme.dart';

/// Chat answers as Markdown, with code blocks that read like code (P2-8).
///
/// The shared `ConversationContent` (lane B, `workspace_ui.dart`) paints the
/// inline-code background on every line inside a fenced block too, which
/// shows as stripes. Here fenced code is formatted by [_PlainCode], so a
/// block is one panel with no per-line background, spans the full reading
/// width and scrolls sideways instead of wrapping; inline code keeps its
/// tint. Once lane B fixes the shared component this can defer to it.
class ChatMarkdown extends StatelessWidget {
  final String content;
  final Color? color;
  const ChatMarkdown({super.key, required this.content, this.color});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final theme = Theme.of(context);
    final body = theme.textTheme.bodyMedium?.copyWith(color: color ?? tokens.text);
    return MarkdownBody(
      data: content,
      selectable: true,
      softLineBreak: true,
      syntaxHighlighter: _PlainCode(tokens.mono(13, color: tokens.text)),
      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
        p: body,
        strong: body?.copyWith(fontWeight: FontWeight.w600),
        h1: theme.textTheme.titleLarge,
        h2: theme.textTheme.titleMedium,
        h3: theme.textTheme.titleSmall,
        a: body?.copyWith(
            color: tokens.accent,
            decoration: TextDecoration.underline,
            decorationColor: tokens.accent.withValues(alpha: 0.5)),
        code: tokens
            .mono(13, color: tokens.text)
            .copyWith(backgroundColor: tokens.raised),
        codeblockPadding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
        codeblockDecoration: BoxDecoration(
            color: tokens.panel,
            borderRadius: BorderRadius.circular(SonderRadius.row),
            border: Border.all(color: tokens.hairline)),
        blockquoteDecoration: BoxDecoration(
            border:
                Border(left: BorderSide(color: tokens.hairlineStrong, width: 2))),
        blockquotePadding: const EdgeInsets.fromLTRB(14, 2, 0, 2),
        horizontalRuleDecoration: BoxDecoration(
            border: Border(top: BorderSide(color: tokens.hairline))),
        blockSpacing: 10,
        listIndent: 22,
      ),
    );
  }
}

/// Formats fenced code with no background, so the block's own panel is the
/// only fill.
class _PlainCode extends SyntaxHighlighter {
  final TextStyle style;
  _PlainCode(this.style);

  @override
  TextSpan format(String source) => TextSpan(style: style, text: source);
}
