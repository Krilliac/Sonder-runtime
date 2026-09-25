import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import '../workspace_ui.dart';
import 'sheet.dart';
import 'status_vocab.dart';
import 'strings.dart';

export 'raise_mode_sheet.dart';
export 'sheet.dart' show showSonderSheet, SonderSheetFrame, ToneButton;

/// What the approval sheet shows about one refused call. This is a view
/// model: the chat lane maps it from the server's refusal receipt
/// (`sonder_receipt.refusal`, server S1) or the approvals list (S2), so the
/// sheet itself never depends on transport types.
@immutable
class ApprovalRequest {
  /// Tool name, `write_file`.
  final String tool;

  /// The ledger call id; shown shortened to 8 characters.
  final String callId;

  /// Redacted argument preview rows, in server order: `path`, `content`.
  /// Values are shown as given (the server already redacts them).
  final List<(String, String)> arguments;

  /// When the call was refused, already formatted (`12:39`).
  final String? refusedAt;

  /// The permission mode that refused it (`manual`).
  final String mode;

  /// Why it was refused, in the server's words (`nobody asked`).
  final String? reason;

  const ApprovalRequest({
    required this.tool,
    required this.callId,
    this.arguments = const [],
    this.refusedAt,
    this.mode = 'manual',
    this.reason,
  });

  /// `12:39 · manual mode · nobody asked`
  String get refusedLine => [
        if (refusedAt != null && refusedAt!.isNotEmpty) refusedAt!,
        '$mode mode',
        if (reason != null && reason!.isNotEmpty) reason!,
      ].join(' · ');
}

/// Lifetimes the sheet offers; the server caps the value it accepts.
const approvalTtlOptions = <Duration>[
  Duration(minutes: 5),
  Duration(minutes: 15),
  Duration(minutes: 60),
];

/// The approval sheet for one refused call (APP-PLAN §2.5).
///
/// ```
/// ? approve   write_file · call 3f9a12c0
///   path     src/render/pso_cache.cpp
///   content  (1,204 chars)
///   refused  12:39 · manual mode · nobody asked
///   Runs this exact call once, from any surface, within 15 minutes. …
///   Valid for  [15 min ▾]
///                                  [Cancel]   [Approve once]
/// ```
///
/// Presentational only: [onApprove] receives the chosen lifetime and the
/// parent performs the request, passing [busy] and [error] back in. When
/// the server has no HTTP approval endpoint, pass [consoleFallback] and the
/// sheet shows `/approve <id>` with Copy instead of the Approve action. The
/// sheet never retries or approves anything by itself.
class ApprovalSheet extends StatefulWidget {
  final ApprovalRequest request;
  final ValueChanged<Duration> onApprove;
  final VoidCallback onCancel;
  final bool busy;
  final String? error;
  final bool consoleFallback;
  final Duration initialTtl;
  final List<Duration> ttlOptions;

  const ApprovalSheet({
    super.key,
    required this.request,
    required this.onApprove,
    required this.onCancel,
    this.busy = false,
    this.error,
    this.consoleFallback = false,
    this.initialTtl = const Duration(minutes: 15),
    this.ttlOptions = approvalTtlOptions,
  });

  @override
  State<ApprovalSheet> createState() => _ApprovalSheetState();
}

class _ApprovalSheetState extends State<ApprovalSheet> {
  late Duration _ttl = widget.ttlOptions.contains(widget.initialTtl)
      ? widget.initialTtl
      : widget.ttlOptions.first;
  bool _copied = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final request = widget.request;
    final keyStyle = tokens.mono(13, color: tokens.muted);
    final valueStyle = tokens.mono(13, color: tokens.text);
    Widget argRow(String key, String value, {Color? color}) => Padding(
          padding: const EdgeInsets.only(bottom: 4),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 76, child: Text(key, style: keyStyle)),
            Expanded(
                child: Text(value,
                    style: color == null
                        ? valueStyle
                        : valueStyle.copyWith(color: color))),
          ]),
        );
    final console = '/approve ${request.callId}';
    return SonderSheetFrame(
      kind: StatusKind.ask,
      word: 'approve',
      title: SonderStrings.approvalTitle(request.tool, request.callId),
      semanticsLabel: SonderStrings.approvalSheetLabel,
      actions: [
        TextButton(
          onPressed: widget.busy ? null : widget.onCancel,
          style: TextButton.styleFrom(minimumSize: const Size(0, 44)),
          child: const Text(SonderStrings.cancel),
        ),
        if (!widget.consoleFallback)
          ToneButton(
            label: SonderStrings.approveOnce,
            role: StatusRole.warning,
            busy: widget.busy,
            onPressed: () => widget.onApprove(_ttl),
          ),
      ],
      children: [
        for (final (key, value) in request.arguments) argRow(key, value),
        argRow(SonderStrings.approvalRefusedLabel, request.refusedLine),
        const SizedBox(height: 12),
        Text(SonderStrings.approvalExplainer(_ttl, request.mode)),
        const SizedBox(height: 12),
        if (widget.consoleFallback)
          WorkspaceNotice(
            kind: StatusKind.note,
            title: SonderStrings.approveFromConsole(request.callId),
            actions: [
              OutlinedButton.icon(
                onPressed: () async {
                  await Clipboard.setData(ClipboardData(text: console));
                  if (mounted) setState(() => _copied = true);
                },
                icon: Icon(_copied ? Icons.check : Icons.copy, size: 16),
                label:
                    Text(_copied ? SonderStrings.copied : SonderStrings.copy),
              ),
            ],
          )
        else
          Row(children: [
            Text(SonderStrings.validFor,
                style: text.bodyMedium?.copyWith(color: tokens.text2)),
            const SizedBox(width: 12),
            DropdownButton<Duration>(
              value: _ttl,
              underline: const SizedBox.shrink(),
              onChanged: widget.busy
                  ? null
                  : (value) {
                      if (value != null) setState(() => _ttl = value);
                    },
              items: [
                for (final option in widget.ttlOptions)
                  DropdownMenuItem(
                      value: option,
                      child: Text(SonderStrings.durationShort(option))),
              ],
            ),
          ]),
        if (widget.error != null) ...[
          const SizedBox(height: 12),
          WorkspaceNotice(kind: StatusKind.fail, title: widget.error!),
        ],
      ],
    );
  }
}

/// Shows the [ApprovalSheet] for [request] and returns the chosen lifetime
/// when the person taps **Approve once**, or null on Cancel/dismiss. The
/// caller then sends exactly one approval request.
Future<Duration?> showApprovalSheet(BuildContext context,
    {required ApprovalRequest request,
    bool consoleFallback = false,
    Duration initialTtl = const Duration(minutes: 15)}) {
  return showSonderSheet<Duration>(
    context,
    builder: (sheetContext) => ApprovalSheet(
      request: request,
      consoleFallback: consoleFallback,
      initialTtl: initialTtl,
      onApprove: (ttl) => Navigator.of(sheetContext).pop(ttl),
      onCancel: () => Navigator.of(sheetContext).pop(),
    ),
  );
}

/// The notice left in the transcript after an approval is issued:
///
/// ```
/// ✓ approved  write_file call 3f9a12c0 once · nonce n_c41a · valid 15 min
///             [Retry the request]   [Revoke]
/// ```
///
/// The request is never retried automatically; [onRetry] is the person's
/// explicit action.
class ApprovalReceipt extends StatelessWidget {
  final String tool;
  final String callId;
  final String? nonce;
  final Duration ttl;
  final VoidCallback? onRetry;
  final VoidCallback? onRevoke;

  const ApprovalReceipt({
    super.key,
    required this.tool,
    required this.callId,
    required this.ttl,
    this.nonce,
    this.onRetry,
    this.onRevoke,
  });

  @override
  Widget build(BuildContext context) {
    return WorkspaceNotice(
      kind: StatusKind.ok,
      word: 'approved',
      title: SonderStrings.approvalReceipt(tool, callId, nonce, ttl),
      actions: [
        if (onRetry != null)
          FilledButton(
              onPressed: onRetry,
              child: const Text(SonderStrings.retryTheRequest)),
        if (onRevoke != null)
          OutlinedButton(
              onPressed: onRevoke, child: const Text(SonderStrings.revoke)),
      ],
    );
  }
}
