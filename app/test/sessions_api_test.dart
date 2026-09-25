// P1-7 (API part): replay and export a server session; list tolerates 404.
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

// Prefix captured from scratchpad/app/parity/history.out; transcript closed.
const _replay = '{"schema": "sonder.http-session-replay.v1", "session_id": '
    '"parity-thread-1", "crash_safe": true, "recovered_sequence": 10, '
    '"integrity_valid": true, "request_present": true, "request_turn_id": '
    '"0fedea52756f4b75b5ad4c6f1b8361ae", "request_snapshot_digest": '
    '"652420b7ebe60cf414bac6f8589f79594b5124a1c956f42e7a40fdef425ad25e", '
    '"transcript": [{"role": "user", "content": "Remember the word PELICAN. '
    'Reply with just OK.", "event_type": "user.message", "sequence": 2, '
    '"turn_id": "48372d22ed6540aa8f159ecbc08baf2e", "name": ""}, {"role": '
    '"assistant", "content": "OK", "event_type": "model.response", '
    '"sequence": 4, "turn_id": "48372d22ed6540aa8f159ecbc08baf2e", "name": ""}, '
    '{"role": "tool", "content": "x", "event_type": "tool.result", '
    '"sequence": 5, "turn_id": "", "name": "read"}]}';

void main() {
  test('replay maps user/assistant turns to chat messages', () async {
    late Uri seen;
    final replay = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .sessions
            .replay('parity-thread-1'),
        () => MockClient((r) async {
              seen = r.url;
              return http.Response(_replay, 200);
            }));
    expect(seen.path, '/v1/sessions/parity-thread-1/replay');
    expect(replay!.integrityValid, isTrue);
    expect(replay.chatMessages.map((m) => '${m.role.name}:${m.content}'), [
      'user:Remember the word PELICAN. Reply with just OK.',
      'assistant:OK',
    ]);
    expect(replay.chatMessages.first.role, Role.user);
  });

  test('export returns the document and a safe file name', () async {
    const body = '{"schema": "sonder.http-session-export.v1", "events": []}';
    final export = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .sessions
            .export('chat 1:x'),
        () => MockClient((r) async => http.Response(body, 200)));
    expect(export!.json, body);
    expect(export.fileName, 'sonder-session-chat_1_x.json');
  });

  test('list is null on a server without S7; 403 is readable', () async {
    final page = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435').sessions.list(),
        () => MockClient(
            (r) async => http.Response('{"error":"not_found"}', 404)));
    expect(page, isNull);
    await expectLater(
      http.runWithClient(
          () =>
              SonderApi(baseUrl: 'http://127.0.0.1:11435').sessions.replay('x'),
          () => MockClient((r) async => http.Response(
              '{"error": {"message": "administrator authorization is required", '
              '"type": "forbidden", "code": "FORBIDDEN"}}',
              403))),
      throwsA(isA<SonderException>().having((e) => e.message, 'message',
          'Only an administrator can open server sessions.')),
    );
  });

  test('path separators are refused without a request', () async {
    var n = 0;
    await http.runWithClient(() async {
      await expectLater(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').sessions.replay('a/b'),
          throwsA(isA<SonderException>()));
    },
        () => MockClient((r) async {
              n++;
              return http.Response('{}', 200);
            }));
    expect(n, 0);
  });
}
