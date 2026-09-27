import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'api.dart';
import 'theme.dart';
import 'ui/status_vocab.dart';
import 'ui/strings.dart';

export 'ui/status_vocab.dart' show StatusKind;

enum WorkspaceDestination {
  chat('Chat', Icons.chat_bubble_outline),
  agents('Agents', Icons.account_tree_outlined),
  runtime('Runtime', Icons.dashboard_customize_outlined),
  settings('Settings', Icons.settings_outlined);

  final String label;
  final IconData icon;
  const WorkspaceDestination(this.label, this.icon);
}

/// Shared peer navigation. Routing remains with the owning chat workspace.
class WorkspaceMenu extends StatelessWidget {
  final WorkspaceDestination current;
  final ValueChanged<WorkspaceDestination> onSelected;
  const WorkspaceMenu(
      {super.key, required this.current, required this.onSelected});

  @override
  Widget build(BuildContext context) => PopupMenuButton<WorkspaceDestination>(
        tooltip: 'Workspace navigation',
        icon: const Icon(Icons.apps_outlined),
        onSelected: onSelected,
        itemBuilder: (_) => [
          for (final destination in WorkspaceDestination.values)
            PopupMenuItem(
                value: destination,
                enabled: destination != current,
                child: Row(children: [
                  Icon(destination.icon, size: 18),
                  const SizedBox(width: 12),
                  Text(destination.label),
                  if (destination == current) ...[
                    const SizedBox(width: 16),
                    const Icon(Icons.check, size: 16)
                  ]
                ]))
        ],
      );
}

class WorkspaceNavigation extends StatelessWidget {
  final WorkspaceDestination current;
  final ValueChanged<WorkspaceDestination> onSelected;
  const WorkspaceNavigation(
      {super.key, required this.current, required this.onSelected});

  @override
  Widget build(BuildContext context) =>
      Column(mainAxisSize: MainAxisSize.min, children: [
        for (final destination in WorkspaceDestination.values)
          ListTile(
              dense: true,
              selected: destination == current,
              leading: Icon(destination.icon, size: 20),
              title: Text(destination.label),
              onTap: destination == current
                  ? null
                  : () => onSelected(destination)),
      ]);
}

/// The legacy three-tone notice API. Kept so existing call sites compile;
/// new code passes a [StatusKind] as `kind:` instead. Mapping: info → note,
/// success → ok, warning → warn.
enum NoticeTone { info, success, warning }

/// Persistent, accessible feedback: the app's port of the REPL notice
/// (style.py `notice`).
///
/// ```
/// ⊘ refused  /write notes.txt
///            File changes need a person to confirm, and manual mode asks first.
///            hint: approve this call once, or change the mode
///            [Approve this call once]  [Change mode…]
/// ```
///
/// The glyph and the kind word always precede the title, in the kind's
/// colour (warn uses `tokens.warn`, ok `tokens.ok`, error and refused
/// `tokens.danger`), so colour never carries the meaning alone. Screen
/// readers hear "refused: /write notes.txt. …": the semantics label starts
/// with the word. The notice is a live region.
class WorkspaceNotice extends StatelessWidget {
  /// The headline. [message] is the legacy name for the same text.
  final String title;

  /// Optional body under the title.
  final String? detail;

  /// Optional next step, drawn muted as `hint: …`.
  final String? hint;

  /// A synonym for the kind's word from the same row of the vocabulary
  /// ("done", "needs you", "off"); defaults to [StatusKind.word].
  final String? word;

  /// Legacy: the tone used when no `kind:` is given.
  final NoticeTone tone;

  /// Legacy single action, drawn under the text.
  final Widget? action;

  /// Buttons under the text, in reading order; the first is the primary.
  final List<Widget> actions;

  final StatusKind? _kind;

  /// Draw the panel and hairline border. Chat's transcript passes false so
  /// a notice sits in the reading column like a turn (plan §2.2).
  final bool framed;

  /// Announce changes to assistive technology. A transcript full of old
  /// notices passes false for all but the newest outcome.
  final bool liveRegion;

  const WorkspaceNotice({
    super.key,
    String? title,
    String? message,
    StatusKind? kind,
    this.tone = NoticeTone.info,
    this.detail,
    this.hint,
    this.word,
    this.action,
    this.actions = const <Widget>[],
    this.framed = true,
    this.liveRegion = true,
  })  : assert(title != null || message != null,
            'WorkspaceNotice needs a title (or the legacy message)'),
        title = title ?? message ?? '',
        _kind = kind;

  /// The legacy name for [title].
  String get message => title;

  /// The notice kind: the explicit `kind:`, else the legacy [tone] mapped.
  StatusKind get kind =>
      _kind ??
      switch (tone) {
        NoticeTone.info => StatusKind.note,
        NoticeTone.success => StatusKind.ok,
        NoticeTone.warning => StatusKind.warn,
      };

  /// The word shown and announced before the title.
  String get kindWord => word ?? kind.word;

  /// What a screen reader hears for the text part of the notice.
  String get semanticsLabel {
    final buffer = StringBuffer('$kindWord: $title');
    final detail = this.detail;
    final hint = this.hint;
    if (detail != null && detail.isNotEmpty) buffer.write('. $detail');
    if (hint != null && hint.isNotEmpty) {
      buffer.write('. ${SonderStrings.hintLabel} $hint');
    }
    return buffer.toString();
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final textTheme = Theme.of(context).textTheme;
    final kind = this.kind;
    final detail = this.detail;
    final hint = this.hint;
    final body = textTheme.bodyMedium?.copyWith(color: tokens.text);
    final buttons = [if (action != null) action!, ...actions];
    return Semantics(
        container: true,
        liveRegion: liveRegion,
        child: Container(
          padding: framed
              ? const EdgeInsets.fromLTRB(12, 10, 12, 10)
              : EdgeInsets.zero,
          decoration: framed
              ? BoxDecoration(
                  color: tokens.panel,
                  border: Border.all(color: tokens.hairline),
                  borderRadius: BorderRadius.circular(SonderRadius.row))
              : null,
          child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Semantics(
                  label: semanticsLabel,
                  excludeSemantics: true,
                  child: Row(
                      crossAxisAlignment: CrossAxisAlignment.baseline,
                      textBaseline: TextBaseline.alphabetic,
                      children: [
                        // Titles start in one column for the common words,
                        // like the REPL's column 11, so a run of notices
                        // reads as a list.
                        ConstrainedBox(
                          constraints: const BoxConstraints(minWidth: 84),
                          child: Text('${kind.glyph} $kindWord',
                              style: tokens.mono(13,
                                  color: kind.color(tokens),
                                  height: 22,
                                  weight: kind.role == StatusRole.danger
                                      ? FontWeight.w600
                                      : FontWeight.w500)),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                            child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                mainAxisSize: MainAxisSize.min,
                                children: [
                              Text(title, style: body),
                              if (detail != null && detail.isNotEmpty)
                                Padding(
                                  padding: const EdgeInsets.only(top: 2),
                                  child: Text(detail,
                                      style: textTheme.bodyMedium
                                          ?.copyWith(color: tokens.text2)),
                                ),
                              if (hint != null && hint.isNotEmpty)
                                Padding(
                                  padding: const EdgeInsets.only(top: 2),
                                  child: Text(
                                      '${SonderStrings.hintLabel} $hint',
                                      style: textTheme.bodySmall
                                          ?.copyWith(color: tokens.muted)),
                                ),
                            ])),
                      ]),
                ),
                if (buttons.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: buttons),
                  ),
              ]),
        ));
  }
}

class RequestFailure {
  final String message;
  final bool retryable, settingsRequired;
  final int? retryAfterSeconds;
  const RequestFailure(this.message,
      {this.retryable = false,
      this.settingsRequired = false,
      this.retryAfterSeconds});

  factory RequestFailure.read(Object error, {required String resource}) {
    if (error is SonderException) {
      switch (error.httpStatus) {
        case 401:
          return const RequestFailure(
              'Your connection needs authentication. Check the account or API key in Settings.',
              settingsRequired: true);
        case 403:
          return RequestFailure(
              'This account cannot access $resource. Check your account in Settings.',
              settingsRequired: true);
        case 404:
          return RequestFailure(
              '$resource is unavailable on this server. It may have been removed or may require a newer server.');
        case 429:
          return RequestFailure(
              'The server is busy. Refresh again after a short wait.',
              retryable: true,
              retryAfterSeconds: error.retryAfterSeconds);
      }
      if (error.httpStatus != null && error.httpStatus! >= 500) {
        return RequestFailure(
            'The server could not refresh $resource. Previously loaded content may be out of date.',
            retryable: true);
      }
      if (error.httpStatus != null &&
          error.httpStatus! < 500 &&
          error.httpStatus != 408) {
        return RequestFailure(error.message);
      }
    }
    if (error is FormatException) {
      return RequestFailure(
          'The server returned an unreadable response for $resource. Check the server version before retrying.');
    }
    if (error is TimeoutException) {
      return RequestFailure(
          'The server took too long to refresh $resource. Previously loaded content is still available.',
          retryable: true);
    }
    return RequestFailure(
        'Could not reach the server to refresh $resource. Check your connection; previously loaded content is still available.',
        retryable: true);
  }
}

const conversationWidth = 760.0;

/// One Markdown owner for chat answers, agent messages and returned reports.
///
/// Code blocks (P2-8): fenced code is drawn on the panel with a transparent
/// text background, so long blocks read as one surface instead of a stripe
/// per line, and scroll horizontally instead of wrapping. Only inline code
/// keeps the raised background. With [fullWidthCode] the blocks take the
/// full available (reading) width; it needs a bounded width, so it is
/// opt-in for callers that lay the content out inside one.
class ConversationContent extends StatelessWidget {
  final String content;
  final Color? color;
  final bool fullWidthCode;
  const ConversationContent(
      {super.key,
      required this.content,
      this.color,
      this.fullWidthCode = false});
  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final body = Theme.of(context)
        .textTheme
        .bodyMedium
        ?.copyWith(color: color ?? tokens.text);
    final blockCode = tokens.mono(13, color: tokens.text);
    return MarkdownBody(
        data: content,
        selectable: true,
        softLineBreak: true,
        fitContent: !fullWidthCode,
        // Used only for fenced/indented blocks: a plain span with no
        // background, so the block has no per-line stripes.
        syntaxHighlighter: _PlainCodeHighlighter(blockCode),
        styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
          p: body,
          strong: body?.copyWith(fontWeight: FontWeight.w600),
          h1: Theme.of(context).textTheme.titleLarge,
          h2: Theme.of(context).textTheme.titleMedium,
          h3: Theme.of(context).textTheme.titleSmall,
          a: body?.copyWith(
              color: tokens.accentText,
              decoration: TextDecoration.underline,
              decorationColor: tokens.accentText.withValues(alpha: 0.5)),
          // Inline code only; blocks use [_PlainCodeHighlighter].
          code: tokens
              .mono(13, color: tokens.text)
              .copyWith(backgroundColor: tokens.raised),
          codeblockPadding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
          codeblockDecoration: BoxDecoration(
              color: tokens.panel,
              borderRadius: BorderRadius.circular(SonderRadius.row),
              border: Border.all(color: tokens.hairline)),
          blockquoteDecoration: BoxDecoration(
              border: Border(
                  left: BorderSide(color: tokens.hairlineStrong, width: 2))),
          blockquotePadding: const EdgeInsets.fromLTRB(14, 2, 0, 2),
          horizontalRuleDecoration: BoxDecoration(
              border: Border(top: BorderSide(color: tokens.hairline))),
          blockSpacing: 10,
          listIndent: 22,
        ));
  }
}

/// Formats a code block as one plain mono span with no background.
class _PlainCodeHighlighter extends SyntaxHighlighter {
  final TextStyle style;
  _PlainCodeHighlighter(this.style);

  @override
  TextSpan format(String source) => TextSpan(style: style, text: source);
}
