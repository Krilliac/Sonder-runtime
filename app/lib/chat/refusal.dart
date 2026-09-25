import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import 'backend.dart';
import 'classify.dart';
import 'notice.dart';

export 'classify.dart' show RefusalInfo, refusalOf, classifyReply, ReplyKind;

/// A refused call, rendered as a notice rather than an answer (P1-2): no
/// rating chips, the refused subject in the title, the server's reason as
/// detail, and "Approve this call once" only when the call id is known.
class RefusalNotice extends StatefulWidget {
  final RefusalInfo refusal;

  /// Sends the approval. Null hides the approve action.
  final Future<ApprovalOutcome> Function(String callId, Duration ttl)? onApprove;

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
    final ttl = await showApprovalSheet(context, widget.refusal);
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
        ChatNotice(
          key: const Key('refusal-notice'),
          kind: ChatStatusKind.refused,
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
        final minutes = (outcome.ttlSeconds > 0
                ? Duration(seconds: outcome.ttlSeconds)
                : _ttl)
            .inMinutes;
        return ChatNotice(
          key: const Key('approval-approved'),
          kind: ChatStatusKind.ok,
          word: 'approved',
          liveRegion: true,
          title: '${r.subject.isEmpty ? 'call' : '${r.subject} call'} '
              '${r.callId} once'
              '${outcome.nonce.isEmpty ? '' : ' · nonce ${outcome.nonce}'}'
              ' · valid $minutes min',
          actions: [
            if (widget.onRetry != null)
              FilledButton.tonal(
                key: const Key('approval-retry'),
                onPressed: widget.onRetry,
                child: const Text('Retry the request'),
              ),
          ],
        );
      case ApprovalStatus.unsupported:
        final command = '/approve ${r.callId}';
        return ChatNotice(
          key: const Key('approval-console'),
          kind: ChatStatusKind.note,
          liveRegion: true,
          title: 'Approve from the console: $command',
          detail: 'This server cannot take approvals from the app yet. Run '
              'the command at the Sonder console on the PC.',
          actions: [
            OutlinedButton.icon(
              key: const Key('approval-copy'),
              icon: const Icon(Icons.copy_all_outlined, size: 16),
              label: const Text('Copy'),
              onPressed: () =>
                  Clipboard.setData(ClipboardData(text: command)),
            ),
          ],
        );
      case ApprovalStatus.forbidden:
        return const ChatNotice(
          key: Key('approval-forbidden'),
          kind: ChatStatusKind.warn,
          liveRegion: true,
          title: 'Approvals need a developer or admin account',
        );
      case ApprovalStatus.failed:
        return ChatNotice(
          key: const Key('approval-failed'),
          kind: ChatStatusKind.fail,
          liveRegion: true,
          title: 'The approval was not recorded',
          detail: outcome.message,
        );
    }
  }
}

/// The §2.5 approval sheet: exactly one call, once. Returns the chosen
/// validity, or null when cancelled. A bottom sheet on phones, a dialog on
/// desktop.
///
/// Lane B owns the shared presentational `lib/ui/approval_sheet.dart`; this
/// is chat's copy of the same layout until that lands.
Future<Duration?> showApprovalSheet(BuildContext context, RefusalInfo r) {
  final wide = MediaQuery.sizeOf(context).width >= 600;
  Widget body(BuildContext ctx) => _ApprovalSheet(refusal: r);
  if (wide) {
    return showDialog<Duration>(
      context: context,
      builder: (ctx) => Dialog(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: body(ctx),
        ),
      ),
    );
  }
  return showModalBottomSheet<Duration>(
    context: context,
    isScrollControlled: true,
    builder: (ctx) => SafeArea(child: body(ctx)),
  );
}

class _ApprovalSheet extends StatefulWidget {
  final RefusalInfo refusal;
  const _ApprovalSheet({required this.refusal});

  @override
  State<_ApprovalSheet> createState() => _ApprovalSheetState();
}

class _ApprovalSheetState extends State<_ApprovalSheet> {
  int _minutes = 15;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final r = widget.refusal;
    final mode = r.mode.isEmpty ? 'the current mode' : r.mode;
    Widget row(String label, String value) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(
                width: 72,
                child: Text(label, style: tokens.mono(12, color: tokens.muted)),
              ),
              Expanded(
                child: Text(value, style: tokens.mono(12, color: tokens.text)),
              ),
            ],
          ),
        );
    return Padding(
      key: const Key('approval-sheet'),
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            header: true,
            label: 'approve: ${r.subject} call ${r.callId}',
            child: ExcludeSemantics(
              child: Text.rich(TextSpan(children: [
                TextSpan(
                    text: '? approve   ',
                    style: tokens.mono(13,
                        color: tokens.warn, weight: FontWeight.w600)),
                TextSpan(
                    text:
                        '${r.subject.isEmpty ? 'call' : r.subject} · call ${r.callId}',
                    style: tokens.mono(13, color: tokens.text)),
              ])),
            ),
          ),
          const SizedBox(height: 12),
          if (r.subject.isNotEmpty) row('call', r.subject),
          if (r.reason.isNotEmpty) row('refused', r.reason),
          row('mode', mode),
          const SizedBox(height: 12),
          Text(
            'Runs this exact call once, from any surface, within $_minutes '
            'minutes. Changing any argument needs a new approval. Your mode '
            'stays $mode.',
            style: text.bodyMedium?.copyWith(color: tokens.text2),
          ),
          const SizedBox(height: 12),
          Row(children: [
            Text('Valid for', style: text.bodySmall),
            const SizedBox(width: 12),
            DropdownButton<int>(
              key: const Key('approval-ttl'),
              value: _minutes,
              items: const [5, 15, 60]
                  .map((m) => DropdownMenuItem(value: m, child: Text('$m min')))
                  .toList(),
              onChanged: (v) => setState(() => _minutes = v ?? 15),
            ),
          ]),
          const SizedBox(height: 12),
          OverflowBar(
            alignment: MainAxisAlignment.end,
            spacing: 8,
            overflowAlignment: OverflowBarAlignment.end,
            children: [
              TextButton(
                key: const Key('approval-cancel'),
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('Cancel'),
              ),
              FilledButton(
                key: const Key('approval-confirm'),
                style: FilledButton.styleFrom(
                  backgroundColor: tokens.warn,
                  foregroundColor: tokens.canvas,
                ),
                onPressed: () =>
                    Navigator.of(context).pop(Duration(minutes: _minutes)),
                child: const Text('Approve once'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
