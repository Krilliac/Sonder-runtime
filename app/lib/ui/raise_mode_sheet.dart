import 'package:flutter/material.dart';

import 'sheet.dart';
import 'status_vocab.dart';
import 'strings.dart';

/// The raise confirmation for a permission-mode switch (APP-PLAN §2.5,
/// P0-4). The server counts the app's `POST /v1/permission-mode` as a
/// person confirming, so this sheet *is* that confirmation.
///
/// ```
/// ! raise mode   manual → auto
///   File changes and host programs will run without asking, for every
///   chat and agent on mypc, until someone lowers the mode.
///   Destructive tools still need a person.
///                                   [Cancel]   [Switch to auto]
/// ```
///
/// The confirm button is danger-toned for `auto` and warn-toned otherwise.
/// Presentational only: the parent sends the request.
class RaiseModeSheet extends StatelessWidget {
  final String from;
  final String to;

  /// The server's display name (`mypc`); empty reads "this server".
  final String host;
  final VoidCallback onConfirm;
  final VoidCallback onCancel;
  final bool busy;

  const RaiseModeSheet({
    super.key,
    required this.from,
    required this.to,
    required this.host,
    required this.onConfirm,
    required this.onCancel,
    this.busy = false,
  });

  @override
  Widget build(BuildContext context) {
    return SonderSheetFrame(
      key: const Key('raise-mode-sheet'),
      kind: StatusKind.warn,
      word: SonderStrings.raiseModeWord,
      title: SonderStrings.raiseTitle(from, to),
      semanticsLabel: '${SonderStrings.raiseModeWord} $from to $to',
      actions: [
        TextButton(
          key: const Key('raise-mode-cancel'),
          onPressed: busy ? null : onCancel,
          style: TextButton.styleFrom(minimumSize: const Size(0, 44)),
          child: const Text(SonderStrings.cancel),
        ),
        ToneButton(
          key: const Key('raise-mode-confirm'),
          label: SonderStrings.switchTo(to),
          role: to == 'auto' ? StatusRole.danger : StatusRole.warning,
          busy: busy,
          onPressed: onConfirm,
        ),
      ],
      children: [
        Text(SonderStrings.raiseEffect(to, host)),
        const SizedBox(height: 8),
        const Text(SonderStrings.raiseDestructiveNote),
      ],
    );
  }
}

/// Shows the [RaiseModeSheet] and returns true only when the person taps
/// **Switch to <mode>**.
Future<bool> showRaiseModeSheet(BuildContext context,
    {required String from, required String to, String host = ''}) async {
  final confirmed = await showSonderSheet<bool>(
    context,
    builder: (sheetContext) => RaiseModeSheet(
      from: from,
      to: to,
      host: host,
      onConfirm: () => Navigator.of(sheetContext).pop(true),
      onCancel: () => Navigator.of(sheetContext).pop(false),
    ),
  );
  return confirmed ?? false;
}

/// The single entry point for a mode change from any surface (picker,
/// Shift+Tab, `/mode`): a raise ([isModeRaise]) asks with the sheet, a
/// lowering or sideways move returns true without asking. Pass [to] exactly
/// as it will be sent to the server; it is resolved the way the server
/// resolves it (case, prefix), and the sheet names the resolved mode.
Future<bool> confirmModeChange(BuildContext context,
    {required String from, required String to, String host = ''}) {
  if (!isModeRaise(from, to)) return Future.value(true);
  return showRaiseModeSheet(context,
      from: resolvePermissionMode(from) ?? from,
      to: resolvePermissionMode(to) ?? to,
      host: host);
}
