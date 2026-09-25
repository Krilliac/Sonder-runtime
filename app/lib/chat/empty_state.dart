import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme.dart';
import '../workspace_ui.dart' show StatusKind, WorkspaceNotice;
import 'connection.dart';
import '../ui/sonder_mark.dart';
import 'drawer.dart' show connectionColor;

/// The empty conversation. Its connection line is driven by the same state
/// as the rail (P0-3): `Connecting…` (muted), `Connected to X` (ok), or the
/// failure with its word plus Retry and Settings.
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
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(
      builder: (context, constraints) {
        final minHeight =
            constraints.maxHeight > 64 ? constraints.maxHeight - 64 : 0.0;
        final pad = constraints.maxWidth < 600 ? 20.0 : 32.0;
        return SingleChildScrollView(
          padding: EdgeInsets.all(pad),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: minHeight),
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const SonderMark(size: 40),
                    const SizedBox(height: 20),
                    Text('Sonder Runtime', style: text.headlineSmall),
                    const SizedBox(height: 8),
                    Text(
                      'Not a standalone model: Sonder Runtime supplies routing, '
                      'prompts, memory, tools, and policy to model weights '
                      'served locally by Ollama.',
                      style: text.bodyMedium?.copyWith(color: tokens.text2),
                    ),
                    const SizedBox(height: 12),
                    ValueListenableBuilder<ConnectionStatus>(
                      valueListenable: connection,
                      builder: (context, c, _) => _ConnectionLine(
                        status: c,
                        onRetry: onRetry,
                        onSettings: onSettings,
                      ),
                    ),
                    const SizedBox(height: 28),
                    Text('Try', style: text.labelSmall),
                    const SizedBox(height: 10),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        _Suggestion(
                            'Write a Python function to parse a CSV', onQuick),
                        _Suggestion('Explain async/await simply', onQuick),
                        _Suggestion('/stats', onQuick),
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
    final tone = connectionColor(tokens, status.state);
    if (status.state == ConnState.connecting ||
        status.state == ConnState.connected) {
      return Semantics(
        key: const Key('empty-connection'),
        label: status.sentence,
        child: ExcludeSemantics(
          child: Row(
            children: [
              Text(status.glyph,
                  style: tokens.mono(12, color: tone, weight: FontWeight.w600)),
              const SizedBox(width: 8),
              Flexible(
                child: Text(
                  status.sentence,
                  style: tokens.mono(12,
                      color: status.isConnected ? tokens.text2 : tokens.muted),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
          ),
        ),
      );
    }
    return OfflineNotice(
      key: const Key('empty-connection'),
      status: status,
      onRetry: onRetry,
      onSettings: onSettings,
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
  final String text;
  final ValueChanged<String> onQuick;
  const _Suggestion(this.text, this.onQuick);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ActionChip(
      label: Text(
        text,
        style: text.startsWith('/')
            ? tokens.mono(12)
            : Theme.of(context).textTheme.labelLarge,
      ),
      onPressed: () => onQuick(text),
    );
  }
}
