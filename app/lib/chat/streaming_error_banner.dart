import 'package:flutter/material.dart';

import '../api.dart' show WorkRun;
import '../api/transport.dart' show SonderException;
import '../models.dart';
import '../theme.dart';
import '../workspace_ui.dart' show StatusKind, WorkspaceNotice;
import 'controller.dart' show ChatEntry, isWorkCapacityMessage;
import 'tool_run_card.dart';
import 'work_run_card.dart' show showRunningWork;

/// Structured classification of a failed or aborted chat turn for clear error
/// recovery messaging and 1-click retry handling.
enum StreamErrorCategory {
  timeout,
  stalled,
  disconnected,
  unreachable,
  cancelled,
  workCapacity,
  generic,
}

/// Visual presentation and recovery metadata for a failed turn.
class StreamErrorPresentation {
  final StreamErrorCategory category;
  final StatusKind kind;

  /// Compact status word shown beside the glyph, or null for the kind default.
  final String? word;

  /// Actionable recovery hint rendered under the error detail, or empty.
  final String recoveryHint;

  /// Whether a transient failure should offer 1-click Retry even when loaded
  /// from saved history (where client-only `ChatEntry.retryable` defaults to
  /// false).
  final bool recoverable;

  const StreamErrorPresentation({
    required this.category,
    required this.kind,
    this.word,
    this.recoveryHint = '',
    this.recoverable = false,
  });
}

/// Classify a stored error [message] using its structured diagnostic lines
/// (`code: TIMEOUT`, `code: STREAM_STALLED`, `code: STREAM_INTERRUPTED`, …)
/// and plain-text copy. Never triggers Retry for 4xx policy/auth failures.
StreamErrorPresentation classifyStreamError(ChatMessage message) {
  if (isWorkCapacityMessage(message)) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.workCapacity,
      kind: StatusKind.warn,
      word: 'busy',
      recoveryHint: 'Stop an active work run or retry once a slot frees up.',
      recoverable: true,
    );
  }
  final diag = message.diagnostic;
  final diagUpper = diag.toUpperCase();
  final contentLower = message.content.toLowerCase();

  bool hasCode(String code) =>
      diagUpper.contains('CODE: $code') || diagUpper.contains('CODE:$code');

  if (hasCode(SonderException.timeoutCode) ||
      hasCode('DEADLINE_EXCEEDED') ||
      hasCode('UPSTREAM_TIMEOUT') ||
      hasCode('STREAM_TIMEOUT') ||
      diagUpper.contains('HTTP 504') ||
      diagUpper.contains('HTTP 408') ||
      contentLower.contains('did not respond in time') ||
      contentLower.contains('took longer than') ||
      contentLower.contains('timed out') ||
      contentLower.contains('deadline')) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.timeout,
      kind: StatusKind.fail,
      word: 'timed out',
      recoveryHint:
          'The turn hit its time limit or the model is still loading. Retry now or switch to a faster route.',
      recoverable: true,
    );
  }

  if (hasCode(SonderException.stalledCode) ||
      contentLower.contains('stopped sending')) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.stalled,
      kind: StatusKind.fail,
      word: 'stalled',
      recoveryHint:
          'No stream updates arrived before the stall deadline. Retry to start a fresh turn.',
      recoverable: true,
    );
  }

  if (hasCode(SonderException.cancelledCode) ||
      contentLower.contains('request cancelled') ||
      contentLower.contains('was cancelled') ||
      contentLower.contains('was stopped')) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.cancelled,
      kind: StatusKind.warn,
      word: 'stopped',
      recoveryHint: 'The turn ended before finishing. Retry to ask again.',
      recoverable: true,
    );
  }

  if (hasCode('STREAM_INTERRUPTED') ||
      contentLower.contains('connection closed before') ||
      contentLower.contains('connection dropped before') ||
      contentLower.contains('stream stopped before')) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.disconnected,
      kind: StatusKind.fail,
      word: 'dropped',
      recoveryHint:
          'The stream disconnected mid-turn. Check the server connection, then retry.',
      recoverable: true,
    );
  }

  if (hasCode(SonderException.unreachableCode) ||
      contentLower.contains('cannot reach') ||
      contentLower.contains('could not reach')) {
    return const StreamErrorPresentation(
      category: StreamErrorCategory.unreachable,
      kind: StatusKind.fail,
      word: 'offline',
      recoveryHint:
          'Make sure the Sonder server is running and reachable, then retry.',
      recoverable: true,
    );
  }

  return const StreamErrorPresentation(
    category: StreamErrorCategory.generic,
    kind: StatusKind.fail,
  );
}

/// Error recovery banner for a failed, timed-out, disconnected, or cancelled
/// turn in the transcript.
///
/// Plain text only (never Markdown, so server URLs never auto-link). Any tool
/// cards that were active before the failure are rendered settled as
/// interrupted instead of disappearing or spinning forever.
class StreamingErrorBanner extends StatelessWidget {
  final ChatEntry entry;
  final String? footer;
  final VoidCallback? onRetry;
  final Future<List<WorkRun>> Function()? listWorkRuns;
  final Future<WorkRun> Function(String id)? cancelWorkRun;
  final Widget copyAction;
  final Widget? detailsWidget;

  const StreamingErrorBanner({
    super.key,
    required this.entry,
    this.footer,
    this.onRetry,
    this.listWorkRuns,
    this.cancelWorkRun,
    required this.copyAction,
    this.detailsWidget,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final message = entry.message;
    final lines = message.content.trim().split('\n');
    final title = lines.first.trim().isEmpty
        ? 'The turn failed before finishing.'
        : lines.first.trim();
    final detail = lines.skip(1).join('\n').trim();
    final info = classifyStreamError(message);
    final isWorkCapacity = info.category == StreamErrorCategory.workCapacity;
    final showRetry = onRetry != null && (entry.retryable || info.recoverable);
    final primaryRetry =
        showRetry && !isWorkCapacity && info.category != StreamErrorCategory.generic;

    // Avoid repeating the hint verbatim when the server message already said it.
    final hint = info.recoveryHint.isEmpty ||
            detail.toLowerCase().contains(info.recoveryHint.toLowerCase())
        ? null
        : info.recoveryHint;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final tool in entry.toolCalls)
          ToolRunCard(
            event: tool,
            turnEnded: true,
            turnFailed: true,
          ),
        WorkspaceNotice(
          framed: false,
          key: const Key('error-notice'),
          kind: info.kind,
          word: info.word,
          liveRegion: true,
          title: title,
          detail: detail.isEmpty ? null : detail,
          hint: hint,
          actions: [
            if (isWorkCapacity &&
                listWorkRuns != null &&
                cancelWorkRun != null)
              FilledButton.tonal(
                key: const Key('error-running-work'),
                onPressed: () => showRunningWork(
                  context,
                  list: listWorkRuns!,
                  cancel: cancelWorkRun!,
                ),
                child: const Text('Show running work'),
              ),
            if (showRetry)
              primaryRetry
                  ? FilledButton.tonalIcon(
                      key: const Key('error-retry'),
                      onPressed: onRetry,
                      icon: const Icon(Icons.refresh, size: 16),
                      label: const Text('Retry'),
                    )
                  : OutlinedButton(
                      key: const Key('error-retry'),
                      onPressed: onRetry,
                      child: const Text('Retry'),
                    ),
            copyAction,
          ],
        ),
        if (footer != null) ...[
          const SizedBox(height: SonderSpace.xs),
          Text(footer!, style: tokens.mono(11, color: tokens.muted)),
        ],
        if (detailsWidget != null) ...[
          const SizedBox(height: SonderSpace.xs),
          detailsWidget!,
        ],
      ],
    );
  }
}
