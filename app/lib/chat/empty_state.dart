import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme.dart';
import '../workspace_ui.dart' show StatusKind, WorkspaceNotice;
import 'connection.dart';
import '../ui/sonder_mark.dart';
import 'drawer.dart' show connectionColor;

/// A starter prompt on the empty conversation.
class ChatSuggestion {
  final IconData icon;

  /// What the chip says.
  final String label;

  /// What it sends; defaults to [label].
  final String? prompt;

  const ChatSuggestion(this.icon, this.label, {this.prompt});

  String get text => prompt ?? label;
}

/// The starter prompts: two plain questions and one command, so the chips
/// also show that slash commands exist.
const chatSuggestions = <ChatSuggestion>[
  ChatSuggestion(Icons.code, 'Write a Python function to parse a CSV'),
  ChatSuggestion(Icons.lightbulb_outline, 'Explain async/await simply'),
  ChatSuggestion(Icons.insights_outlined, 'Show runtime stats',
      prompt: '/stats'),
];

/// The empty conversation: a calm welcome, the connection line and starter
/// prompts. The connection line is driven by the same state as the rail
/// (P0-3): `Connecting…` (muted), `Connected to X` (ok), or the failure
/// with its word plus Retry and Settings.
class ChatEmptyState extends StatelessWidget {
  final ValueListenable<ConnectionStatus> connection;
  final ValueChanged<String> onQuick;
  final VoidCallback onRetry;
  final VoidCallback onSettings;

  const ChatEmptyState({
    super.key,
    required this.connection,
    required this.onQuick,
    required this.onRetry,
    required this.onSettings,
  });

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(
      builder: (context, constraints) {
        final narrow = constraints.maxWidth < 600;
        final pad = narrow ? SonderSpace.xl : SonderSpace.x3;
        final minHeight = constraints.maxHeight > 2 * pad
            ? constraints.maxHeight - 2 * pad
            : 0.0;
        return SingleChildScrollView(
          padding: EdgeInsets.all(pad),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: minHeight),
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 560),
                child: Column(
                  key: const Key('chat-empty-state'),
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const SonderMark(size: 44),
                    const SizedBox(height: SonderSpace.xl),
                    Semantics(
                      header: true,
                      child: Text('What should we work on?',
                          textAlign: TextAlign.center,
                          style: text.headlineSmall),
                    ),
                    const SizedBox(height: SonderSpace.md),
                    ValueListenableBuilder<ConnectionStatus>(
                      valueListenable: connection,
                      builder: (context, c, _) => _ConnectionLine(
                        status: c,
                        onRetry: onRetry,
                        onSettings: onSettings,
                      ),
                    ),
                    const SizedBox(height: SonderSpace.x3),
                    Wrap(
                      alignment: WrapAlignment.center,
                      spacing: SonderSpace.sm,
                      runSpacing: SonderSpace.xs,
                      children: [
                        for (final s in chatSuggestions)
                          _Suggestion(suggestion: s, onQuick: onQuick),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

class _ConnectionLine extends StatelessWidget {
  final ConnectionStatus status;
  final VoidCallback onRetry;
  final VoidCallback onSettings;

  const _ConnectionLine({
    required this.status,
    required this.onRetry,
    required this.onSettings,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final tone = connectionColor(tokens, status.state);
    if (status.state == ConnState.connecting ||
        status.state == ConnState.connected) {
      return Semantics(
        key: const Key('empty-connection'),
        label: status.sentence,
        child: ExcludeSemantics(
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(status.glyph,
                  style: tokens.mono(12, color: tone, weight: FontWeight.w600)),
              const SizedBox(width: SonderSpace.sm),
              Flexible(
                child: Text(
                  status.sentence,
                  style: text.bodySmall?.copyWith(
                      color: status.isConnected ? tokens.text2 : tokens.muted),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
          ),
        ),
      );
    }
    // A failure gets its notice on a card, left-aligned for reading.
    return Container(
      key: const Key('empty-connection'),
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.lg, SonderSpace.md, SonderSpace.lg, SonderSpace.md),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.card),
        border: Border.all(color: tokens.hairline),
      ),
      child: OfflineNotice(
        status: status,
        onRetry: onRetry,
        onSettings: onSettings,
      ),
    );
  }
}

/// `! refused  mypc.local refused this address` or `✗ error  Can't reach
/// mypc`, with the remedy and Retry / Settings (P0-3, P2-13).
class OfflineNotice extends StatelessWidget {
  final ConnectionStatus status;
  final VoidCallback onRetry;
  final VoidCallback onSettings;

  const OfflineNotice({
    super.key,
    required this.status,
    required this.onRetry,
    required this.onSettings,
  });

  @override
  Widget build(BuildContext context) {
    final kind = status.state == ConnState.unreachable
        ? StatusKind.fail
        : StatusKind.warn;
    return WorkspaceNotice(
      framed: false,
      kind: kind,
      // The REPL's words: "✗ error  Can't reach mypc", "! refused  …".
      word: status.state == ConnState.unreachable ? 'error' : status.word,
      liveRegion: true,
      title: status.sentence,
      detail: status.remedy,
      actions: [
        OutlinedButton(
          key: const Key('connection-retry'),
          onPressed: onRetry,
          child: const Text('Retry'),
        ),
        TextButton(
          key: const Key('connection-settings'),
          onPressed: onSettings,
          child: const Text('Settings'),
        ),
      ],
    );
  }
}

class _Suggestion extends StatelessWidget {
  final ChatSuggestion suggestion;
  final ValueChanged<String> onQuick;
  const _Suggestion({required this.suggestion, required this.onQuick});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return ActionChip(
      key: Key('suggestion-${suggestion.text}'),
      avatar: Icon(suggestion.icon, size: 16, color: tokens.text2),
      label: Text(suggestion.label,
          style: text.labelLarge?.copyWith(color: tokens.text)),
      tooltip: suggestion.prompt,
      backgroundColor: tokens.panel,
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.xs, vertical: SonderSpace.xs),
      onPressed: () => onQuick(suggestion.text),
    );
  }
}
