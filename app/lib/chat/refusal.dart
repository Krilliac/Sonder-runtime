import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../ui/approval_sheet.dart'
    show ApprovalReceipt, ApprovalRequest, showApprovalSheet;
import '../workspace_ui.dart' show StatusKind, WorkspaceNotice;
import 'backend.dart';
import 'classify.dart';

export 'classify.dart' show RefusalInfo, refusalOf, classifyReply, ReplyKind;

/// A refused call, rendered as a notice rather than an answer (P1-2): no
/// rating chips, the refused subject in the title, the server's reason as
/// detail, and "Approve this call once" only when the call id is known.
class RefusalNotice extends StatefulWidget {
  final RefusalInfo refusal;

  /// Sends the approval. Null hides the approve action.
  final Future<ApprovalOutcome> Function(String callId, Duration ttl)?
      onApprove;

  /// Opens the mode picker.
  final VoidCallback? onChangeMode;

  /// Re-sends the refused request after an approval.
  final VoidCallback? onRetry;

  const RefusalNotice({
    super.key,
    required this.refusal,
    this.onApprove,
    this.onChangeMode,
    this.onRetry,
  });

  @override
  State<RefusalNotice> createState() => _RefusalNoticeState();
}

class _RefusalNoticeState extends State<RefusalNotice> {
  ApprovalOutcome? _outcome;
  Duration _ttl = const Duration(minutes: 15);
  bool _busy = false;

  Future<void> _approve() async {
    final callId = widget.refusal.callId;
    final approve = widget.onApprove;
    if (callId.isEmpty || approve == null || _busy) return;
    final ttl = await showRefusalApprovalSheet(context, widget.refusal);
    if (ttl == null || !mounted) return;
    setState(() => _busy = true);
    final outcome = await approve(callId, ttl);
    if (!mounted) return;
    setState(() {
      _busy = false;
      _ttl = ttl;
      _outcome = outcome;
    });
  }

  @override
  Widget build(BuildContext context) {
    final r = widget.refusal;
    final title = r.subject.isNotEmpty ? r.subject : 'this request';
    final canApprove = r.callId.isNotEmpty && widget.onApprove != null;
    final outcome = _outcome;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        WorkspaceNotice(
          framed: false,
          liveRegion: false,
          key: const Key('refusal-notice'),
          kind: StatusKind.refused,
          title: title,
          detail: r.reason,
          hint: r.mode.isEmpty ? '' : 'mode: ${r.mode}',
          actions: [
            if (canApprove && outcome == null)
              FilledButton.tonal(
                key: const Key('refusal-approve'),
                onPressed: _busy ? null : _approve,
                child: Text(_busy ? 'Approving…' : 'Approve this call once'),
              ),
            if (widget.onChangeMode != null)
              OutlinedButton(
                key: const Key('refusal-change-mode'),
                onPressed: widget.onChangeMode,
                child: const Text('Change mode…'),
              ),
          ],
        ),
        if (outcome != null) ...[
          const SizedBox(height: 10),
          _outcomeView(context, outcome, r),
        ],
      ],
    );
  }

  Widget _outcomeView(
      BuildContext context, ApprovalOutcome outcome, RefusalInfo r) {
    switch (outcome.status) {
      case ApprovalStatus.approved:
        return ApprovalReceipt(
          key: const Key('approval-approved'),
          tool: r.subject.isEmpty ? 'call' : r.subject,
          callId: r.callId,
          nonce: outcome.nonce,
          ttl: outcome.ttlSeconds > 0
              ? Duration(seconds: outcome.ttlSeconds)
              : _ttl,
          onRetry: widget.onRetry,
        );
      case ApprovalStatus.unsupported:
        final command = '/approve ${r.callId}';
        return WorkspaceNotice(
          framed: false,
          key: const Key('approval-console'),
          kind: StatusKind.note,
          liveRegion: true,
          title: 'Approve from the console: $command',
          detail: 'This server cannot take approvals from the app yet. Run '
              'the command at the Sonder console on the PC.',
          actions: [
            OutlinedButton.icon(
              key: const Key('approval-copy'),
              icon: const Icon(Icons.copy_all_outlined, size: 16),
              label: const Text('Copy'),
              onPressed: () => Clipboard.setData(ClipboardData(text: command)),
            ),
          ],
        );
      case ApprovalStatus.forbidden:
        return const WorkspaceNotice(
          framed: false,
          key: Key('approval-forbidden'),
          kind: StatusKind.warn,
          liveRegion: true,
          title: 'Approvals need a developer or admin account',
        );
      case ApprovalStatus.failed:
        return WorkspaceNotice(
          framed: false,
          key: const Key('approval-failed'),
          kind: StatusKind.fail,
          liveRegion: true,
          title: 'The approval was not recorded',
          detail: outcome.message,
        );
    }
  }
}

/// The §2.5 approval sheet for [r], drawn by lane B's [ApprovalSheet]:
/// exactly one call, once. Returns the chosen validity, or null when
/// cancelled.
Future<Duration?> showRefusalApprovalSheet(
        BuildContext context, RefusalInfo r) =>
    showApprovalSheet(context, request: approvalRequestFor(r));

/// The approval sheet's view model for a refusal.
ApprovalRequest approvalRequestFor(RefusalInfo r) => ApprovalRequest(
      tool: r.subject.isEmpty ? 'call' : r.subject,
      callId: r.callId,
      mode: r.mode.isEmpty ? 'the current' : r.mode,
      reason: r.reason.isEmpty ? null : r.reason,
    );
