/// The Review list behind the Overview's Approvals row (plan §2.5): pending
/// calls a gate refused and open one-call approvals, from `GET /v1/approvals`
/// (server S2). A server without the route gets the console fallback.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'runtime_data.dart';
import 'status_word.dart';
import 'work_runs_panel.dart';

class ApprovalsPanel extends StatelessWidget {
  final ApprovalsPage? page;
  final Object? error;
  const ApprovalsPanel({super.key, required this.page, this.error});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final failure = error;
    if (failure is SonderException &&
        (failure.httpStatus == 403 || failure.httpStatus == 401)) {
      return const RuntimePanelNote(
          status: RuntimeStatus.skipped,
          word: 'n/a',
          text: 'Approvals need a developer or admin account.');
    }
    if (failure != null) {
      return RuntimePanelNote(
          status: RuntimeStatus.fail,
          text: failure is SonderException
              ? failure.message
              : 'Could not load approvals.');
    }
    final current = page;
    if (current == null) {
      return const RuntimePanelNote(
          status: RuntimeStatus.unknown, text: 'Not loaded yet.');
    }
    if (!current.supported) {
      return const RuntimePanelNote(
          status: RuntimeStatus.skipped,
          word: 'n/a',
          text: 'This server cannot approve over HTTP. Approve from the '
              'console with /approve <call id>.');
    }
    if (current.pending.isEmpty && current.open.isEmpty) {
      return const RuntimePanelNote(
          status: RuntimeStatus.ok, word: 'ok', text: 'Nothing waiting.');
    }
    Widget row(RuntimeStatus status, String? word, List<String> parts) =>
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 3),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            RuntimeStatusWord(status, word: word, width: 116),
            Expanded(
              child: Text(
                parts.join(' · '),
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(12, color: tokens.text2),
              ),
            ),
          ]),
        );
    Widget pendingRow(PendingApproval item) => row(RuntimeStatus.ask, null, [
          item.tool.isEmpty ? 'call' : item.tool,
          if (item.callId.isNotEmpty) 'call ${item.callId}',
          if (item.preview.isNotEmpty) item.preview,
        ]);
    Widget openRow(IssuedApproval item) =>
        row(RuntimeStatus.ok, 'approved', [
          item.tool.isEmpty ? 'call' : item.tool,
          if (item.callId.isNotEmpty) 'call ${item.callId}',
          if (item.ttlSeconds > 0) '${item.ttlSeconds}s once',
        ]);
    return Column(
      key: const Key('approvals-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final item in current.pending) pendingRow(item),
        for (final item in current.open) openRow(item),
      ],
    );
  }
}
