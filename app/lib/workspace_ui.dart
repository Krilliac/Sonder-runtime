import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'api.dart';
import 'theme.dart';

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

enum NoticeTone { info, success, warning }

/// Persistent, accessible feedback shared by connection and conversation UIs.
class WorkspaceNotice extends StatelessWidget {
  final String message;
  final NoticeTone tone;
  final Widget? action;
  const WorkspaceNotice(
      {super.key,
      required this.message,
      this.tone = NoticeTone.info,
      this.action});
  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final color = tone == NoticeTone.warning ? tokens.danger : tokens.accent;
    return Semantics(
        liveRegion: true,
        child: Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
              color: tokens.panel,
              border: Border.all(color: tokens.hairline),
              borderRadius: BorderRadius.circular(SonderRadius.row)),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(
                switch (tone) {
                  NoticeTone.warning => Icons.error_outline,
                  NoticeTone.success => Icons.check_circle_outline,
                  _ => Icons.info_outline
                },
                size: 18,
                color: color),
            const SizedBox(width: 10),
            Expanded(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [Text(message), if (action != null) action!])),
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
class ConversationContent extends StatelessWidget {
  final String content;
  final Color? color;
  const ConversationContent({super.key, required this.content, this.color});
  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final body = Theme.of(context)
        .textTheme
        .bodyMedium
        ?.copyWith(color: color ?? tokens.text);
    return MarkdownBody(
        data: content,
        selectable: true,
        softLineBreak: true,
        styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
          p: body,
          strong: body?.copyWith(fontWeight: FontWeight.w600),
          h1: Theme.of(context).textTheme.titleLarge,
          h2: Theme.of(context).textTheme.titleMedium,
          h3: Theme.of(context).textTheme.titleSmall,
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
