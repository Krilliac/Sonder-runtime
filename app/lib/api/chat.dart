/// Chat reply DTO and the completion-to-reply mapping shared by the JSON and
/// streaming paths.
library;

import '../models.dart';

/// One assistant turn: the answer, plus the model's reasoning when the
/// deployment exposes it. [reasoning] is empty in the normal case.
class ChatReply {
  final String text;
  final String reasoning;
  final ChatResponseMetadata? metadata;

  const ChatReply({required this.text, this.reasoning = '', this.metadata});

  bool get hasReasoning => reasoning.trim().isNotEmpty;

  /// The work run still producing this answer, or empty when [text] is the
  /// answer. While non-empty, [text] is a neutral placeholder, never the
  /// server's raw `GET /v1/work-runs/…` instructions.
  String get pendingWorkRunId =>
      metadata?.workRunning == true ? metadata!.workRunId : '';

  /// The refusal carried by this turn, or null.
  ChatRefusal? get refusal => metadata?.refusal;
}

Map<String, dynamic> _map(Object? value) =>
    value is Map ? Map<String, dynamic>.from(value) : const <String, dynamic>{};

/// Neutral text shown in place of the server's pending-run instructions.
String workRunPlaceholder(String runId) =>
    'Work is still running on the server (work run $runId). '
    'The answer will appear here when it finishes.';

/// Build the metadata for one completion.
///
/// [completion] is the final JSON object (non-stream) or a merge of the
/// finish/usage chunks (stream). [headers] supplies `x-sonder-*` fallbacks.
ChatResponseMetadata chatMetadataFrom(
  Map<String, dynamic> completion, {
  Map<String, String> headers = const {},
  String finishReason = '',
  String content = '',
}) {
  final receipt = _map(completion['sonder_receipt']);
  final usage = _map(completion['usage']);
  final activity = _map(completion['sonder_activity']);
  final work = _map(receipt['chat_work']);
  final headerElapsed = int.tryParse(headers['x-sonder-elapsed-ms'] ?? '');
  final refusalJson = receipt['refusal'] is Map
      ? _map(receipt['refusal'])
      : ChatRefusal.fromText(content)?.toJson();
  return ChatResponseMetadata.fromJson({
    'completion_id': completion['id'],
    'request_id': receipt['request_id'] ?? headers['x-sonder-correlation-id'],
    'model': receipt['model'] ?? completion['model'],
    'tier': receipt['tier'],
    'finish_reason': finishReason,
    'status': activity['status'],
    'cache': receipt['cache'],
    'elapsed_ms': receipt['elapsed_ms'] ??
        completion['sonder_elapsed_ms'] ??
        headerElapsed,
    'prompt_tokens': usage['prompt_tokens'],
    'completion_tokens': usage['completion_tokens'],
    'total_tokens': usage['total_tokens'],
    'model_calls': activity['model_calls'],
    'tool_calls': activity['tool_calls'],
    'work_run_id': work['work_run_id'],
    'work_status': work['status'],
    if (refusalJson != null) 'refusal': refusalJson,
  });
}

/// Assemble a [ChatReply] from answer text and its completion metadata.
ChatReply chatReplyFrom({
  required String content,
  required Map<String, dynamic> completion,
  Map<String, String> headers = const {},
  String finishReason = '',
  String warning = '',
}) {
  final reply = content.trimRight();
  final reasoning = completion['sonder_reasoning']?.toString().trim() ?? '';
  final metadata = chatMetadataFrom(
    completion,
    headers: headers,
    finishReason: finishReason,
    content: reply,
  );
  final text =
      metadata.workRunning ? workRunPlaceholder(metadata.workRunId) : reply;
  return ChatReply(
    text: warning.isEmpty ? text : '$warning\n\n$text',
    reasoning: reasoning,
    metadata: metadata.isEmpty ? null : metadata,
  );
}
