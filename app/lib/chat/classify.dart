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
// The server's hand-off sentence (serve.py `_work_run_pending_text`), and
// the app's own placeholder (api/chat.dart `workRunPlaceholder`), anchored
// at the start of the reply. A reply that merely mentions a run is an answer.
final RegExp _workRunHandOff =
    RegExp(r'^Work is still running (?:as work run|on the server \(work run) '
        r'(wr-[0-9a-f]{8,64})\b');
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
  if (structured != null &&
      (structured.tool.isNotEmpty || structured.callId.isNotEmpty)) {
    // The server's receipt is the only authority for what was refused and
    // which ledger call "Approve once" would approve. The reply text is
    // model-authored and may name a different tool, reason or call id.
    return RefusalInfo(
      subject: structured.tool,
      reason: structured.reason,
      mode: structured.mode,
      callId: structured.callId,
    );
  }
  var body = head == null ? text : text.substring(head.end);
  var mode = '';
  final firstLine = body.split('\n').first;
  final tail = _modeTail.firstMatch(firstLine);
  if (tail != null) {
    mode = tail.group(1) ?? '';
    body =
        firstLine.substring(0, tail.start) + body.substring(firstLine.length);
  }
  // Text-only refusals (servers without S1) are shown as notices but never
  // offer approval: a `/approve <id>` in model text is not authority.
  return RefusalInfo(
    subject: head?.group(1) ?? '',
    reason: body.trim(),
    mode: mode,
  );
}

/// The work run [message] hands off to, or null.
///
/// Prefers lane A's `sonder_receipt.chat_work` metadata; falls back to the
/// server's hand-off sentence ("Work is still running as work run wr-…"),
/// which must lead the reply, for older servers.
WorkRunRef? workRunOf(ChatMessage message) {
  if (message.role != Role.assistant || message.error || message.pending) {
    return null;
  }
  // Lane A's `sonder_receipt.chat_work` metadata names the run directly,
  // and is authoritative whenever it names one: a settled run is no
  // hand-off, whatever the text says.
  final metadata = message.responseMetadata;
  if (metadata != null && metadata.workRunId.isNotEmpty) {
    if (!metadata.workRunning) return null;
    final budget =
        int.tryParse(_budget.firstMatch(message.content)?.group(1) ?? '');
    return WorkRunRef(metadata.workRunId, budgetSeconds: budget);
  }
  final text = message.content.trimLeft();
  // The hand-off is short and leads the reply; an answer that merely
  // mentions a run id is an answer.
  if (text.length > 1200) return null;
  final id = _workRunHandOff.firstMatch(text)?.group(1);
  if (id == null) return null;
  final budget = int.tryParse(_budget.firstMatch(text)?.group(1) ?? '');
  return WorkRunRef(id, budgetSeconds: budget);
}
