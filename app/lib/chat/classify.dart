import '../models.dart';

/// What an assistant reply *is*, before it is rendered.
///
/// A refusal must never read like an answer (it gets no rating chips), and a
/// long workbench turn's hand-off text is not an answer either: it is a
/// pointer to a work run the app can fetch itself.
enum ReplyKind { answer, refused, error, workRun }

/// A permission gate refused a call.
///
/// Built from `sonder_activity.status` / `sonder_receipt.refusal` once server
/// S1 lands (lane A's `ChatResponseMetadata.refusal`); until then from the
/// server's stable wording: `refused /write: <reason> (mode: manual)` and,
/// when the approval ledger has the call, `/approve 3f9a12c0`.
class RefusalInfo {
  /// What was refused, e.g. `/write`; empty when the server did not say.
  final String subject;
  final String reason;
  final String mode;

  /// The approval ledger id, when known. "Approve once" is offered only then.
  final String callId;

  const RefusalInfo({
    this.subject = '',
    this.reason = '',
    this.mode = '',
    this.callId = '',
  });
}

/// A long HTTP workbench turn that outlived the server's wait.
class WorkRunRef {
  final String id;

  /// Wall-clock budget from the hand-off text, when present.
  final int? budgetSeconds;

  const WorkRunRef(this.id, {this.budgetSeconds});

  /// `wr-7c1e…` — enough to tell runs apart in a sentence.
  String get shortId => id.length <= 7 ? id : '${id.substring(0, 7)}…';
}

final RegExp _refusedHead =
    RegExp(r'^refused(?:\s+(/?[^\s:]+))?\s*:\s*', caseSensitive: true);
final RegExp _modeTail = RegExp(r'\s*\(mode:\s*([A-Za-z]+)\)\s*\.?\s*$');
final RegExp _approveId = RegExp(r'/approve\s+([0-9a-f]{8,})');
final RegExp _workRunId = RegExp(r'\b(wr-[0-9a-f]{8,64})\b');
final RegExp _workRunHandOff =
    RegExp(r'(work run wr-[0-9a-f]+|/v1/work-runs/wr-[0-9a-f]+)');
final RegExp _budget = RegExp(r'wall-clock budget (\d+)\s*s');

/// Classify a stored assistant message. Pure and cheap; the transcript
/// caches the result per message anyway.
ReplyKind classifyReply(ChatMessage message) {
  if (message.role != Role.assistant) return ReplyKind.answer;
  if (message.error) return ReplyKind.error;
  if (workRunOf(message) != null) return ReplyKind.workRun;
  if (refusalOf(message) != null) return ReplyKind.refused;
  return ReplyKind.answer;
}

/// The refusal carried by [message], or null when it is not one.
RefusalInfo? refusalOf(ChatMessage message) {
  if (message.role != Role.assistant || message.error || message.pending) {
    return null;
  }
  final text = message.content.trim();
  final head = _refusedHead.firstMatch(text);
  final status = message.responseMetadata?.status ?? '';
  // Server S1 puts the refusal in `sonder_receipt.refusal` (lane A's
  // ChatRefusal); the text patterns are the fallback for older servers.
  final structured = message.responseMetadata?.refusal;
  if (head == null && status != 'refused' && structured == null) return null;
  var body = head == null ? text : text.substring(head.end);
  var mode = '';
  final approve = _approveId.firstMatch(text);
  final firstLine = body.split('\n').first;
  final tail = _modeTail.firstMatch(firstLine);
  if (tail != null) {
    mode = tail.group(1) ?? '';
    body =
        firstLine.substring(0, tail.start) + body.substring(firstLine.length);
  }
  final structuredId = structured?.callId ?? '';
  final structuredTool = structured?.tool ?? '';
  return RefusalInfo(
    subject: head?.group(1) ?? structuredTool,
    reason: body.trim(),
    mode: mode,
    callId: structuredId.isNotEmpty ? structuredId : approve?.group(1) ?? '',
  );
}

/// The work run [message] hands off to, or null.
///
/// Prefers lane A's `sonder_receipt.chat_work` metadata; falls back to the
/// server's hand-off sentence ("Work is still running as work run wr-…
/// Fetch the answer with GET /v1/work-runs/wr-…") for older servers.
WorkRunRef? workRunOf(ChatMessage message) {
  if (message.role != Role.assistant || message.error || message.pending) {
    return null;
  }
  // Lane A's `sonder_receipt.chat_work` metadata names the run directly.
  final metadata = message.responseMetadata;
  if (metadata != null && metadata.workRunning) {
    final budget =
        int.tryParse(_budget.firstMatch(message.content)?.group(1) ?? '');
    return WorkRunRef(metadata.workRunId, budgetSeconds: budget);
  }
  final text = message.content;
  if (!text.contains('work run') && !text.contains('/v1/work-runs/')) {
    return null;
  }
  if (!_workRunHandOff.hasMatch(text)) return null;
  // The hand-off is short and leads the reply; a long answer that merely
  // mentions a run id is an answer.
  if (text.length > 1200) return null;
  final id = _workRunId.firstMatch(text)?.group(1);
  if (id == null) return null;
  final budget = int.tryParse(_budget.firstMatch(text)?.group(1) ?? '');
  return WorkRunRef(id, budgetSeconds: budget);
}
