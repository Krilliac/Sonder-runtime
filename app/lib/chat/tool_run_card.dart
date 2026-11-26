import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';

import '../api/stream.dart' show ChatStreamToolEvent, ToolRunResult;
import '../theme.dart';
import '../ui/status_line.dart' show compactCount;
import '../ui/status_row.dart' show StatusMark;
import '../ui/strings.dart';
import '../workspace_ui.dart' show StatusKind, WorkspaceNotice;
import 'backend.dart' show ApprovalOutcome, ApprovalStatus;

/// Callback to approve or deny a paused `sonder_tool` call inline without
/// re-submitting the prompt.
typedef ToolDecisionCallback = Future<ApprovalOutcome> Function(
    String callId, String tool, String digest);

/// Detects bidirectional override/isolate or zero-width hidden characters
/// client-side as defense-in-depth alongside the server's `bidi_warning`.
List<String> detectHiddenBidiChars(String text) {
  if (text.isEmpty) return const [];
  final found = <String>{};
  for (final rune in text.runes) {
    final isBidi = (rune >= 0x202A && rune <= 0x202E) ||
        (rune >= 0x2066 && rune <= 0x2069) ||
        rune == 0x200E ||
        rune == 0x200F ||
        (rune >= 0x200B && rune <= 0x200D) ||
        rune == 0xFEFF;
    if (isBidi) {
      found.add('U+${rune.toRadixString(16).toUpperCase().padLeft(4, '0')}');
    }
  }
  return found.toList(growable: false);
}

/// Renders one `sonder_tool` event in a chat turn:
///
/// * Exact tool name, optional language, and shortened call id;
/// * Host (`unsandboxed`) vs container (`isolated`) trust badge;
/// * Bidi / hidden-character warning banner when present;
/// * Exact code or redacted arguments/preview;
/// * Inline **Approve once** (`POST /v1/approvals/<call_id>`) and **Deny**
///   (`POST /v1/approvals/deny/<call_id>`) buttons while paused in
///   `awaiting_approval`, without re-submitting the prompt;
/// * Execution result or refusal/error details once settled.
class ToolRunCard extends StatefulWidget {
  final ChatStreamToolEvent event;
  final ToolDecisionCallback? onApprove;
  final ToolDecisionCallback? onDeny;
  final VoidCallback? onChangeMode;

  /// True once the enclosing turn is no longer streaming. Prevents synthetic
  /// `delta.tool_calls` cards (which stream with `state: 'running'`) or
  /// aborted SSE tool calls from staying stuck on a running indicator.
  final bool turnEnded;

  /// True when the enclosing turn ended with an error, timeout, or disconnect.
  final bool turnFailed;

  const ToolRunCard({
    super.key,
    required this.event,
    this.onApprove,
    this.onDeny,
    this.onChangeMode,
    this.turnEnded = false,
    this.turnFailed = false,
  });

  @override
  State<ToolRunCard> createState() => _ToolRunCardState();
}

class _ToolRunCardState extends State<ToolRunCard> {
  final ScrollController _codeScroll = ScrollController();
  bool _codeDetached = false;
  bool _codeScrollScheduled = false;
  bool _busy = false;
  String _pendingAction = '';
  String _submittedNotice = '';
  ApprovalOutcome? _outcome;

  @override
  void dispose() {
    _codeScroll.dispose();
    super.dispose();
  }

  @override
  void didUpdateWidget(covariant ToolRunCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.event.callId != widget.event.callId ||
        oldWidget.event.state != widget.event.state ||
        oldWidget.turnEnded != widget.turnEnded ||
        oldWidget.turnFailed != widget.turnFailed) {
      _busy = false;
      _pendingAction = '';
      if (!widget.event.isAwaitingApproval &&
          !widget.event.isAwaitingClientApproval) {
        _submittedNotice = '';
        _outcome = null;
      }
    }
    final oldTextLen =
        oldWidget.event.code.length + oldWidget.event.detail.length;
    final newTextLen = widget.event.code.length + widget.event.detail.length;
    if (!widget.turnEnded &&
        widget.event.state == 'running' &&
        newTextLen > oldTextLen &&
        !_codeDetached) {
      _scrollCodeToEnd();
    }
  }

  void _scrollCodeToEnd() {
    if (_codeScrollScheduled) return;
    _codeScrollScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _codeScrollScheduled = false;
      if (!mounted || _codeDetached || !_codeScroll.hasClients) return;
      final position = _codeScroll.position;
      if (position.userScrollDirection == ScrollDirection.forward) {
        _codeDetached = true;
        return;
      }
      if (position.maxScrollExtent - position.pixels > 0.5) {
        position.jumpTo(position.maxScrollExtent);
      }
    });
  }

  bool _onCodeScroll(ScrollNotification notification) {
    final distance =
        notification.metrics.maxScrollExtent - notification.metrics.pixels;
    if (notification is UserScrollNotification) {
      if (notification.direction == ScrollDirection.forward) {
        _codeDetached = true;
      } else if (notification.direction == ScrollDirection.reverse &&
          distance <= 24) {
        _codeDetached = false;
      }
    } else if (notification is ScrollUpdateNotification) {
      final delta = notification.scrollDelta ?? 0;
      if (delta < -0.5 && distance > 2) {
        _codeDetached = true;
      } else if (delta > 0 && distance <= 24) {
        _codeDetached = false;
      }
    } else if (notification is ScrollEndNotification && distance <= 4) {
      _codeDetached = false;
    }
    return false;
  }

  String _effectiveState(ChatStreamToolEvent e) {
    if (!widget.turnEnded) return e.state;
    final inProgress = e.state == 'running' ||
        e.state == 'approved' ||
        e.state == 'awaiting_approval' ||
        (widget.turnFailed && e.state == 'awaiting_client_approval');
    if (!inProgress) return e.state;
    if (widget.turnFailed) return 'interrupted';
    if (!e.isClientTarget) return 'done';
    return e.state;
  }

  Future<void> _decide(
    String action,
    ToolDecisionCallback? callback,
  ) async {
    final callId = widget.event.callId;
    if (callId.isEmpty || callback == null || _busy) return;
    setState(() {
      _busy = true;
      _pendingAction = action;
      _outcome = null;
    });
    final outcome =
        await callback(callId, widget.event.tool, widget.event.digest);
    if (!mounted) return;
    final isClient = widget.event.isClientTarget;
    setState(() {
      _busy = false;
      _pendingAction = '';
      if (outcome.status == ApprovalStatus.approved) {
        _submittedNotice = action == 'approve'
            ? (isClient
                ? 'Executed on your PC — resuming turn…'
                : 'Approved once — waiting for execution…')
            : (isClient
                ? 'Denied on your PC — returning refusal to the turn…'
                : 'Denied — returning refusal to the turn…');
        _outcome = null;
      } else {
        _outcome = outcome;
      }
    });
  }

  (StatusKind, String) _statusFor(String state) => switch (state) {
        'awaiting_approval' => (StatusKind.ask, 'approve?'),
        'awaiting_client_approval' => (StatusKind.ask, 'approve on PC?'),
        'approved' => (StatusKind.ok, 'approved'),
        'running' => (StatusKind.running, 'running'),
        'done' => (StatusKind.ok, 'done'),
        'denied' => (StatusKind.refused, 'denied'),
        'expired' => (StatusKind.warn, 'expired'),
        'refused' => (StatusKind.refused, 'refused'),
        'interrupted' => (StatusKind.fail, 'interrupted'),
        'error' => (StatusKind.fail, 'error'),
        _ => (StatusKind.note, state.isEmpty ? 'tool' : state),
      };

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final e = widget.event;
    final effectiveState = _effectiveState(e);
    final (kind, word) = _statusFor(effectiveState);
    final detectedChars = <String>{
      ...e.bidiChars,
      ...detectHiddenBidiChars(e.code),
      ...detectHiddenBidiChars(e.preview),
      ...detectHiddenBidiChars(e.detail),
      for (final val in e.args.values) ...detectHiddenBidiChars(val),
    }.toList(growable: false);
    final showBidi = e.bidiWarning || detectedChars.isNotEmpty;
    final awaitingDecision = effectiveState == 'awaiting_approval' ||
        (e.isClientTarget && effectiveState == 'awaiting_client_approval');
    final liveStreamingDelta = !widget.turnEnded &&
        effectiveState == 'running' &&
        e.result == null &&
        e.error == null &&
        e.code.isEmpty &&
        e.detail.isNotEmpty;
    // Approve is offered only next to the payload it approves.
    final canAct = awaitingDecision &&
        e.hasPayload &&
        e.callId.isNotEmpty &&
        _submittedNotice.isEmpty &&
        (widget.onApprove != null || widget.onDeny != null);

    return Container(
      key: Key('tool-run-card-${e.id.isEmpty ? e.tool : e.id}'),
      margin: const EdgeInsets.only(bottom: SonderSpace.sm),
      padding: const EdgeInsets.all(SonderSpace.md),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.card),
        border: Border.all(
          color: awaitingDecision
              ? tokens.warn
              : (effectiveState == 'interrupted'
                  ? tokens.danger
                  : tokens.hairline),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: SonderSpace.sm,
            runSpacing: SonderSpace.xs,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              StatusMark(kind, word: word, size: 12),
              Text(
                e.tool.isEmpty ? 'tool' : e.tool,
                style: tokens.mono(13,
                    color: tokens.text, weight: FontWeight.w600),
              ),
              if (e.language.isNotEmpty)
                Text(
                  e.language,
                  style: tokens.mono(12, color: tokens.muted),
                ),
              if (e.callId.isNotEmpty)
                Text(
                  'call ${SonderStrings.shortCallId(e.callId)}',
                  style: tokens.mono(12, color: tokens.muted),
                ),
              if (liveStreamingDelta)
                Text(
                  'streaming · ${compactCount(e.detail.length)} chars',
                  key: const Key('tool-run-live-delta'),
                  style: tokens.mono(11, color: tokens.info),
                ),
              if (e.backend.isNotEmpty) _BackendTrustBadge(backend: e.backend),
            ],
          ),
          if (showBidi) ...[
            const SizedBox(height: SonderSpace.sm),
            WorkspaceNotice(
              key: const Key('tool-run-bidi-warning'),
              framed: false,
              liveRegion: false,
              kind: StatusKind.warn,
              title: 'Hidden bidirectional or control characters detected',
              detail: detectedChars.isEmpty
                  ? 'Inspect the exact payload before approving.'
                  : 'Found ${detectedChars.join(', ')}. Inspect carefully before approving.',
            ),
          ],
          if (e.code.isNotEmpty || e.detail.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            Container(
              key: const Key('tool-run-code'),
              constraints: const BoxConstraints(maxHeight: 220),
              padding: const EdgeInsets.all(SonderSpace.sm),
              decoration: BoxDecoration(
                color: tokens.sunken,
                borderRadius: BorderRadius.circular(SonderRadius.control),
                border: Border.all(color: tokens.hairline),
              ),
              child: NotificationListener<ScrollNotification>(
                onNotification: _onCodeScroll,
                child: SingleChildScrollView(
                  controller: _codeScroll,
                  child: SelectableText(
                    e.code.isNotEmpty ? e.code : e.detail,
                    style: tokens.mono(12, color: tokens.text),
                  ),
                ),
              ),
            ),
          ] else if (e.args.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            Container(
              key: const Key('tool-run-args'),
              padding: const EdgeInsets.all(SonderSpace.sm),
              decoration: BoxDecoration(
                color: tokens.sunken,
                borderRadius: BorderRadius.circular(SonderRadius.control),
                border: Border.all(color: tokens.hairline),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (final entry in e.args.entries)
                    Padding(
                      padding: const EdgeInsets.only(bottom: SonderSpace.xxs),
                      child: SelectableText(
                        '${entry.key}: ${entry.value}',
                        style: tokens.mono(12, color: tokens.text),
                      ),
                    ),
                ],
              ),
            ),
          ] else if (e.preview.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            Container(
              key: const Key('tool-run-preview'),
              padding: const EdgeInsets.all(SonderSpace.sm),
              decoration: BoxDecoration(
                color: tokens.sunken,
                borderRadius: BorderRadius.circular(SonderRadius.control),
                border: Border.all(color: tokens.hairline),
              ),
              child: SelectableText(
                e.preview,
                style: tokens.mono(12, color: tokens.text),
              ),
            ),
          ] else if (e.argumentsReported) ...[
            const SizedBox(height: SonderSpace.sm),
            Text(
              'No arguments.',
              key: const Key('tool-run-no-arguments'),
              style: tokens.mono(12, color: tokens.text2),
            ),
          ],
          if (e.detailTruncated) ...[
            const SizedBox(height: SonderSpace.xs),
            Text(
              'Only the first part is shown (${e.detailChars} characters in '
              'all). Deny it if you cannot judge the rest.',
              key: const Key('tool-run-truncated'),
              style: tokens.mono(12, color: tokens.warn),
            ),
          ],
          if (awaitingDecision && !e.hasPayload) ...[
            const SizedBox(height: SonderSpace.sm),
            Text(
              'This request did not say what it would run, so it cannot be '
              'approved here. Review it in the approvals list.',
              key: const Key('tool-run-no-payload'),
              style: tokens.mono(12, color: tokens.warn),
            ),
          ],
          if (_submittedNotice.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            Text(
              _submittedNotice,
              key: const Key('tool-run-submitted'),
              style: tokens.mono(12, color: tokens.text2),
            ),
          ],
          if (canAct) ...[
            const SizedBox(height: SonderSpace.md),
            Wrap(
              spacing: SonderSpace.sm,
              runSpacing: SonderSpace.xs,
              children: [
                if (widget.onApprove != null)
                  FilledButton.tonal(
                    key: const Key('tool-run-approve'),
                    onPressed:
                        _busy ? null : () => _decide('approve', widget.onApprove),
                    child: Text(_busy && _pendingAction == 'approve'
                        ? (e.isClientTarget ? 'Running on PC…' : 'Approving…')
                        : (e.isClientTarget
                            ? 'Approve on my PC'
                            : SonderStrings.approveOnce)),
                  ),
                if (widget.onDeny != null)
                  OutlinedButton(
                    key: const Key('tool-run-deny'),
                    style: OutlinedButton.styleFrom(
                      foregroundColor: tokens.danger,
                    ),
                    onPressed:
                        _busy ? null : () => _decide('deny', widget.onDeny),
                    child: Text(_busy && _pendingAction == 'deny'
                        ? 'Denying…'
                        : 'Deny'),
                  ),
                if (widget.onChangeMode != null)
                  TextButton(
                    key: const Key('tool-run-change-mode'),
                    onPressed: _busy ? null : widget.onChangeMode,
                    child: const Text('Change mode…'),
                  ),
              ],
            ),
          ],
          if (_outcome != null) ...[
            const SizedBox(height: SonderSpace.sm),
            _decisionOutcomeView(_outcome!, e.callId),
          ],
          if (e.error != null && e.error!.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.sm),
            Text(
              e.error!,
              key: const Key('tool-run-error'),
              style: tokens.mono(12, color: tokens.danger),
            ),
          ],
          if (e.result != null) ...[
            const SizedBox(height: SonderSpace.sm),
            _ToolResultView(result: e.result!),
          ],
        ],
      ),
    );
  }

  Widget _decisionOutcomeView(ApprovalOutcome outcome, String callId) {
    switch (outcome.status) {
      case ApprovalStatus.approved:
        return const SizedBox.shrink();
      case ApprovalStatus.unsupported:
        final command = SonderStrings.approveCommand(callId);
        return WorkspaceNotice(
          framed: false,
          key: const Key('tool-run-console-fallback'),
          kind: StatusKind.note,
          title: 'Approve from the console: $command',
          actions: [
            OutlinedButton.icon(
              icon: const Icon(Icons.copy_all_outlined, size: 16),
              label: const Text(SonderStrings.copy),
              onPressed: () => Clipboard.setData(ClipboardData(text: command)),
            ),
          ],
        );
      case ApprovalStatus.forbidden:
        return WorkspaceNotice(
          framed: false,
          key: const Key('tool-run-forbidden'),
          kind: StatusKind.warn,
          title: outcome.message.isNotEmpty
              ? outcome.message
              : SonderStrings.approvalsNeedRole,
        );
      case ApprovalStatus.failed:
        return WorkspaceNotice(
          framed: false,
          key: const Key('tool-run-outcome-failed'),
          kind: StatusKind.fail,
          title: 'Could not record decision',
          detail: outcome.message,
        );
    }
  }
}

class _BackendTrustBadge extends StatelessWidget {
  final String backend;
  const _BackendTrustBadge({required this.backend});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final isHost = backend == 'host';
    final isContainer = backend == 'container';
    final isClient = backend == 'client';
    final color = isHost
        ? tokens.warn
        : (isContainer || isClient ? tokens.ok : tokens.muted);
    final label = isHost
        ? 'host · unsandboxed'
        : (isContainer
            ? 'container · isolated'
            : (isClient ? 'client PC' : backend));
    final icon = isHost
        ? Icons.warning_amber_rounded
        : (isContainer
            ? Icons.shield_outlined
            : (isClient ? Icons.computer_outlined : Icons.memory_outlined));
    return Container(
      key: const Key('tool-run-backend-badge'),
      padding: const EdgeInsets.symmetric(
        horizontal: SonderSpace.sm,
        vertical: SonderSpace.xxs,
      ),
      decoration: BoxDecoration(
        color: tokens.sunken,
        borderRadius: BorderRadius.circular(SonderRadius.pill),
        border: Border.all(color: color),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 12, color: color),
          const SizedBox(width: SonderSpace.xs),
          Flexible(
            child: Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: tokens.mono(11, color: color, weight: FontWeight.w600),
            ),
          ),
        ],
      ),
    );
  }
}

class _ToolResultView extends StatelessWidget {
  final ToolRunResult result;
  const _ToolResultView({required this.result});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final summary = <String>[
      result.ok ? 'ok' : 'failed',
      if (result.returncode != null) 'exit ${result.returncode}',
      if (result.timedOut) 'timed out',
      if (result.elapsedMs != null) '${result.elapsedMs}ms',
      if (result.truncated) 'truncated',
    ].join(' · ');
    final body = result.output.isNotEmpty
        ? result.output
        : [
            if (result.stdoutTail.isNotEmpty) result.stdoutTail,
            if (result.stderrTail.isNotEmpty) result.stderrTail,
          ].join('\n');
    return Column(
      key: const Key('tool-run-result'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          summary,
          style: tokens.mono(
            11,
            color: result.ok ? tokens.ok : tokens.danger,
            weight: FontWeight.w500,
          ),
        ),
        if (body.isNotEmpty) ...[
          const SizedBox(height: SonderSpace.xs),
          Container(
            constraints: const BoxConstraints(maxHeight: 160),
            padding: const EdgeInsets.all(SonderSpace.sm),
            decoration: BoxDecoration(
              color: tokens.sunken,
              borderRadius: BorderRadius.circular(SonderRadius.control),
              border: Border.all(color: tokens.hairline),
            ),
            child: SingleChildScrollView(
              child: SelectableText(
                body,
                style: tokens.mono(12, color: tokens.text2),
              ),
            ),
          ),
        ],
      ],
    );
  }
}
