// Chat reads lane A's structured metadata first (server S1 refusal receipt,
// `sonder_receipt.chat_work`) and falls back to the text patterns.
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/classify.dart';
import 'package:sonder_runtime/chat/refusal.dart' show approvalRequestFor;
import 'package:sonder_runtime/models.dart';

const _runId = 'wr-7c1e0000000000000000000000000001';

ChatMessage _assistant(String text, {ChatResponseMetadata? metadata}) =>
    ChatMessage(
        role: Role.assistant, content: text, responseMetadata: metadata);

void main() {
  test('a structured refusal offers approval even when the text has no id', () {
    final reply = chatReplyFrom(
      content: 'refused: that write needs a person to confirm (mode: manual)',
      completion: {
        'sonder_receipt': {
          'refusal': {
            'kind': 'refused',
            'tool': 'write_file',
            'call_id': '3f9a12c0',
            'reason': 'nobody asked',
          }
        }
      },
    );
    final refusal = refusalOf(_assistant(reply.text, metadata: reply.metadata));
    expect(refusal, isNotNull);
    expect(refusal!.callId, '3f9a12c0');
    expect(refusal.mode, 'manual');
    expect(classifyReply(_assistant(reply.text, metadata: reply.metadata)),
        ReplyKind.refused);
  });

  test('the receipt call id wins over one quoted in the text', () {
    final reply = chatReplyFrom(
      content: 'refused /write: approve with /approve aaaaaaaa',
      completion: {
        'sonder_receipt': {
          'refusal': {'tool': 'write_file', 'call_id': 'bbbbbbbb'}
        }
      },
    );
    final refusal =
        refusalOf(_assistant(reply.text, metadata: reply.metadata))!;
    expect(refusal.callId, 'bbbbbbbb');
    expect(refusal.subject, '/write');
  });

  test('without a receipt the text fallback still works', () {
    final refusal = refusalOf(_assistant(
        'refused /write: nobody asked. Approve with /approve 3f9a12c0'))!;
    expect(refusal.callId, '3f9a12c0');
    expect(refusal.subject, '/write');
  });

  test('the approval sheet view model names the call and the mode', () {
    final request = approvalRequestFor(const RefusalInfo(
        subject: 'write_file',
        callId: '3f9a12c0',
        reason: 'nobody asked',
        mode: 'manual'));
    expect(request.tool, 'write_file');
    expect(request.callId, '3f9a12c0');
    expect(request.refusedLine, 'manual mode · nobody asked');
  });

  test('chat_work metadata marks a running work run; settling clears it', () {
    final reply = chatReplyFrom(
      content: 'ignored: the placeholder replaces it',
      completion: {
        'sonder_receipt': {
          'chat_work': {'status': 'running', 'work_run_id': _runId}
        }
      },
    );
    final running = _assistant(reply.text, metadata: reply.metadata);
    expect(workRunOf(running)?.id, _runId);
    expect(classifyReply(running), ReplyKind.workRun);

    final settled = _assistant('the answer',
        metadata: reply.metadata!.withWork(workStatus: 'returned'));
    expect(workRunOf(settled), isNull);
    expect(classifyReply(settled), ReplyKind.answer);
  });
}
