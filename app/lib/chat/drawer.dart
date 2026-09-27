import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';
import '../ui/sonder_mark.dart';
import '../workspace_ui.dart';
import 'connection.dart';

/// Colour for a connection state; always paired with its word.
Color connectionColor(SonderTokens tokens, ConnState state) => switch (state) {
      ConnState.connected => tokens.ok,
      ConnState.connecting => tokens.muted,
      ConnState.refused ||
      ConnState.unauthorized ||
      ConnState.serverError =>
        tokens.warn,
      ConnState.unreachable => tokens.danger,
    };

/// `✓ 127.0.0.1:11435 connected` / `! mypc.local refused` / `✗ can't reach`.
class ConnectionRow extends StatelessWidget {
  final ValueListenable<ConnectionStatus> connection;
  const ConnectionRow({super.key, required this.connection});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<ConnectionStatus>(
      valueListenable: connection,
      builder: (context, c, _) {
        final tone = connectionColor(tokens, c.state);
        return Tooltip(
          message: c.sentence,
          child: Semantics(
            key: const Key('rail-connection'),
            label: c.sentence,
            child: ExcludeSemantics(
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(c.glyph,
                      style: tokens.mono(11,
                          color: tone, weight: FontWeight.w600)),
                  const SizedBox(width: 6),
                  Flexible(
                    child: Text(c.host,
                        overflow: TextOverflow.ellipsis,
                        style: tokens.mono(11, color: tokens.muted)),
                  ),
                  const SizedBox(width: 6),
                  Text(c.word, style: tokens.mono(11, color: tone)),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

class ChatDrawer extends StatelessWidget {
  final List<ChatThread> threads;
  final String currentThreadId;
  final VoidCallback onNew;
  final ValueChanged<ChatThread> onSelect;
  final ValueChanged<ChatThread> onDelete;
  final bool embedded;
  final ValueListenable<ConnectionStatus>? connection;
  final ValueChanged<WorkspaceDestination>? onNavigate;
  final VoidCallback? onOpenCommands;
  final VoidCallback? onOpenRuntime;
  final VoidCallback? onOpenSettings;

  const ChatDrawer({
    super.key,
    required this.threads,
    required this.currentThreadId,
    required this.onNew,
    required this.onSelect,
    required this.onDelete,
    this.embedded = false,
    this.connection,
    this.onNavigate,
    this.onOpenCommands,
    this.onOpenRuntime,
    this.onOpenSettings,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final projects = threads.map((t) => t.project).toSet().toList()..sort();
    return Drawer(
      shape:
          embedded ? Border(right: BorderSide(color: tokens.hairline)) : null,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (embedded)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 11, 8, 4),
                child: Row(
                  children: [
                    const SonderMark(),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text('Sonder',
                              style: text.titleSmall?.copyWith(
                                  fontWeight: FontWeight.w600, height: 1.1)),
                          Text('Local-first workspace',
                              style: tokens.mono(10, color: tokens.muted)),
                        ],
                      ),
                    ),
                    IconButton(
                      tooltip: 'New chat',
                      onPressed: onNew,
                      icon: const Icon(Icons.add, size: 18),
                    ),
                  ],
                ),
              )
            else
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 8, 4),
                child: Row(
                  children: [
                    Expanded(child: Text('Chats', style: text.titleSmall)),
                    IconButton(
                      tooltip: 'New chat',
                      onPressed: () {
                        unawaited(Navigator.of(context).maybePop());
                        onNew();
                      },
                      icon: const Icon(Icons.add_comment_outlined, size: 18),
                    ),
                  ],
                ),
              ),
            if (embedded)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 2),
                child: Text('Chats', style: text.labelSmall),
              ),
            Expanded(
              child: threads.isEmpty
                  ? Center(
                      child: Text('No chats yet',
                          style: text.bodySmall?.copyWith(color: tokens.muted)),
                    )
                  : ListView.builder(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 4),
                      itemCount: threads.length,
                      itemBuilder: (_, index) {
                        final thread = threads[index];
                        return ThreadRow(
                          key: ValueKey(thread.id),
                          thread: thread,
                          selected: thread.id == currentThreadId,
                          onTap: () => onSelect(thread),
                          onDelete: threads.length <= 1
                              ? null
                              : () => onDelete(thread),
                        );
                      },
                    ),
            ),
            if (onNavigate != null)
              WorkspaceNavigation(
                  current: WorkspaceDestination.chat,
                  onSelected: (destination) {
                    if (!embedded) Navigator.of(context).pop();
                    onNavigate!(destination);
                  }),
            if (projects.isNotEmpty) ...[
              Divider(height: 1, color: tokens.hairline),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 4),
                child: Text('Projects', style: text.labelSmall),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final project in projects.take(4))
                      Padding(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 8, vertical: 5),
                        child: Row(
                          children: [
                            Container(
                              width: 7,
                              height: 7,
                              decoration: BoxDecoration(
                                color: tokens.hairlineStrong,
                                borderRadius: BorderRadius.circular(4),
                              ),
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Text(project,
                                  overflow: TextOverflow.ellipsis,
                                  style: text.bodySmall
                                      ?.copyWith(color: tokens.text2)),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
            ],
            if (embedded) ...[
              Divider(height: 1, color: tokens.hairline),
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 6, 12, 8),
                child: Row(
                  children: [
                    if (onNavigate == null)
                      IconButton(
                        tooltip: 'Runtime',
                        onPressed: onOpenRuntime,
                        icon: const Icon(Icons.dashboard_customize_outlined),
                      ),
                    IconButton(
                      tooltip: 'Commands',
                      onPressed: onOpenCommands,
                      icon: const Icon(Icons.bolt_outlined),
                    ),
                    if (onNavigate == null)
                      IconButton(
                        tooltip: 'Settings',
                        onPressed: onOpenSettings,
                        icon: const Icon(Icons.settings_outlined),
                      ),
                    const Spacer(),
                    if (connection != null)
                      Flexible(
                        flex: 4,
                        child: ConnectionRow(connection: connection!),
                      ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// One conversation in the rail: its title and its turn count. Delete
/// stays behind a real button with a label and a 48 dp target.
class ThreadRow extends StatelessWidget {
  final ChatThread thread;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback? onDelete;

  const ThreadRow({
    super.key,
    required this.thread,
    required this.selected,
    required this.onTap,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final n = thread.messages.length;
    return Semantics(
      selected: selected,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        child: Container(
          constraints: const BoxConstraints(minHeight: 48),
          padding: const EdgeInsets.only(left: 10, right: 0),
          decoration: BoxDecoration(
            color: selected ? tokens.raised : null,
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  thread.displayTitle,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: text.bodySmall?.copyWith(
                    fontSize: 13,
                    color: selected ? tokens.text : tokens.text2,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Tooltip(
                message: '$n message${n == 1 ? '' : 's'}',
                child: Text('$n', style: tokens.mono(11, color: tokens.muted)),
              ),
              IconButton(
                tooltip: 'Delete chat',
                onPressed: onDelete,
                icon: Icon(Icons.close, size: 14, color: tokens.muted),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
