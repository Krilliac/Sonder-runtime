/// The approvals queue (plan §2.5): calls a permission gate refused because
/// nobody could be asked, and the one-call approvals already issued, from
/// `GET /v1/approvals` (server S2).
///
/// Every action follows UX-CONTRACT.md: **Approve once…** re-reads the
/// server's pending entry and opens the approval sheet drawn from it (the
/// screen owns that flow), the POST is bound to that entry's tool and
/// digest, and nothing is retried automatically. **Revoke** cancels an open
/// approval. A server without the route gets the console fallback.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/strings.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'runtime_rows.dart';
import 'status_word.dart';

/// `14m left`, `expired`, or null when the server sent no expiry.
String? approvalTimeLeft(IssuedApproval item, DateTime now) {
  final expires = item.expiresAt;
  if (expires == null) {
    return item.ttlSeconds > 0
        ? 'valid ${SonderStrings.durationWords(Duration(seconds: item.ttlSeconds))}'
        : null;
  }
  final left = expires.difference(now);
  if (left.isNegative) return 'expired';
  return '${compactDuration(left)} left';
}

/// `2 waiting · 1 approved`, or null before the first read.
String? approvalsSummary(ApprovalsPage? page) {
  if (page == null || !page.supported) return null;
  final pending = page.pending.length;
  final open = page.open.length;
  if (pending == 0 && open == 0) return 'Nothing waiting';
  return [
    if (pending > 0) '$pending waiting',
    if (open > 0) '$open approved once',
  ].join(' · ');
}

class ApprovalsPanel extends StatelessWidget {
  final ApprovalsPage? page;
  final Object? error;
  final bool loading;
  final DateTime? now;
  final VoidCallback? onRefresh;

  /// Re-reads the call on the server and opens the approval sheet. Null
  /// hides **Approve once…**.
  final Future<void> Function(PendingApproval item)? onApprove;

  /// Revokes an open approval. Null hides **Revoke**.
  final Future<void> Function(IssuedApproval item)? onRevoke;

  /// Call ids being checked or approved, and nonces being revoked.
  final Set<String> busy;

  /// Call ids whose approval POST is in flight (after the sheet).
  final Set<String> approving;

  /// The outcome of the last action, by call id (approve) or nonce
  /// (revoke). An outcome whose row has gone shows at the top.
  final Map<String, ActionOutcome> outcomes;
  final ValueChanged<String>? onDismiss;

  const ApprovalsPanel({
    super.key,
    required this.page,
    this.error,
    this.loading = false,
    this.now,
    this.onRefresh,
    this.onApprove,
    this.onRevoke,
    this.busy = const {},
    this.approving = const {},
    this.outcomes = const {},
    this.onDismiss,
  });

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      key: const Key('approvals-panel'),
      title: 'Approvals',
      description: approvalsSummary(page) ??
          'Calls a permission gate refused because nobody could be asked.',
      trailing: onRefresh == null
          ? null
          : IconButton(
              tooltip: 'Refresh approvals',
              onPressed: loading ? null : onRefresh,
              icon: const Icon(Icons.refresh, size: 18),
            ),
      children: _rows(context),
    );
  }

  Widget? _outcomeFor(String key) {
    final outcome = outcomes[key];
    if (outcome == null) return null;
    return OutcomeView(outcome,
        onDismiss: onDismiss == null ? null : () => onDismiss!(key));
  }

  List<Widget> _rows(BuildContext context) {
    final failure = error;
    if (failure is SonderException &&
        (failure.httpStatus == 403 || failure.httpStatus == 401)) {
      return const [
        RuntimePanelNote(
            status: StatusKind.skipped,
            word: 'n/a',
            text: 'Approvals need a developer or admin account.'),
      ];
    }
    if (failure != null) {
      return [
        RuntimePanelNote(
          status: StatusKind.fail,
          text: failure is SonderException
              ? failure.message
              : 'Could not load approvals.',
          action: onRefresh == null
              ? null
              : TextButton(onPressed: onRefresh, child: const Text('Retry')),
        ),
      ];
    }
    final current = page;
    if (current == null) {
      return [
        loading
            ? const SkeletonRows(rows: 2, semanticLabel: 'Loading approvals')
            : const RuntimePanelNote(
                status: StatusKind.unknown, text: 'Not loaded yet.'),
      ];
    }
    if (!current.supported) {
      return const [
        RuntimePanelNote(
            status: StatusKind.skipped,
            word: 'n/a',
            text: 'This server cannot approve over HTTP. Approve from the '
                'console with /approve <call id>.'),
      ];
    }
    final clock = now ?? DateTime.now();
    final shown = <String>{
      for (final item in current.pending) item.callId,
      for (final item in current.open) ...[item.callId, item.nonce],
    };
    return [
      // An outcome whose row is gone (approved, spent or expired since).
      for (final entry in outcomes.entries)
        if (!shown.contains(entry.key))
          Padding(
            padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.lg, vertical: SonderSpace.md),
            child: OutcomeView(entry.value,
                onDismiss:
                    onDismiss == null ? null : () => onDismiss!(entry.key)),
          ),
      if (current.pending.isEmpty && current.open.isEmpty)
        const RuntimeEmptyRow('Nothing waiting.',
            icon: Icons.verified_user_outlined),
      for (final item in current.pending) _pendingRow(context, item),
      for (final item in current.open) _openRow(context, item, clock),
    ];
  }

  Widget _callTitle(BuildContext context, String tool, String callId) {
    final tokens = SonderTokens.of(context);
    return Text.rich(
      TextSpan(children: [
        TextSpan(
            text: tool.isEmpty ? 'call' : tool,
            style: tokens.mono(13, weight: FontWeight.w500)),
        if (callId.isNotEmpty)
          TextSpan(
              text: '  call ${SonderStrings.shortCallId(callId)}',
              style: tokens.mono(12, color: tokens.muted)),
      ]),
      maxLines: 1,
      overflow: TextOverflow.ellipsis,
    );
  }

  Widget _pendingRow(BuildContext context, PendingApproval item) {
    final refused = [
      if (item.refusedAt != null) 'refused ${clockLabel(item.refusedAt!)}',
      if (item.mode.isNotEmpty) '${item.mode} mode',
    ].join(' · ');
    final isApproving = approving.contains(item.callId);
    return RuntimeRow(
      key: Key('approval-pending-${item.callId}'),
      kind: StatusKind.ask,
      title: _callTitle(context, item.tool, item.callId),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (item.preview.isNotEmpty)
            RuntimeRowDetail(item.preview, mono: true, maxLines: 3),
          if (refused.isNotEmpty) RuntimeRowDetail(refused, maxLines: 1),
        ],
      ),
      actions: [
        if (onApprove != null)
          AsyncActionButton(
            buttonKey: Key('approval-review-${item.callId}'),
            label: 'Approve once…',
            busyLabel: isApproving ? 'Approving…' : 'Checking…',
            doneLabel: null,
            busy: busy.contains(item.callId) || isApproving,
            onPressed: () => onApprove!(item),
            onError: (_, __) {},
          ),
      ],
      below: _outcomeFor(item.callId),
    );
  }

  Widget _openRow(BuildContext context, IssuedApproval item, DateTime now) {
    final left = approvalTimeLeft(item, now);
    final outcome = _outcomeFor(
        item.callId.isNotEmpty && outcomes.containsKey(item.callId)
            ? item.callId
            : item.nonce);
    return RuntimeRow(
      key: Key('approval-open-${item.nonce}'),
      kind: StatusKind.ok,
      word: 'approved',
      title: _callTitle(context, item.tool, item.callId),
      subtitle: RuntimeRowDetail(['once', if (left != null) left].join(' · '),
          maxLines: 1),
      actions: [
        if (onRevoke != null)
          AsyncActionButton(
            buttonKey: Key('approval-revoke-${item.nonce}'),
            label: SonderStrings.revoke,
            busyLabel: 'Revoking…',
            doneLabel: null,
            style: ActionButtonStyle.text,
            busy: busy.contains(item.nonce),
            onPressed: () => onRevoke!(item),
            onError: (_, __) {},
          ),
      ],
      below: outcome,
    );
  }
}
