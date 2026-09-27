// P0-8 (API part): a long workbench turn surfaces its work run instead of the
// server's raw `GET /v1/work-runs/<id>` instructions; WorkRunsApi fetches the
// answer, lists runs and cancels one.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

const _runId = 'wr-7c1e9a4b2d6f40c8a3e5b1d7f9c2e4a6';
const _hi = [ChatMessage(role: Role.user, content: 'refactor the PSO cache')];

void main() {
  test('a running chat_work receipt yields a work run, not raw API text',
      () async {
    final reply = await http.runWithClient(
      () => SonderApi(baseUrl: 'http://127.0.0.1:11435').chatDetailed(_hi),
      () => MockClient(
          (_) async => fixtureResponse('chat_work_running.json', 200)),
    );
    expect(reply.pendingWorkRunId, _runId);
    expect(reply.metadata!.workRunId, _runId);
    expect(reply.metadata!.workStatus, 'running');
    expect(reply.metadata!.workRunning, isTrue);
    expect(reply.text, isNot(contains('GET /v1/work-runs')));
    expect(reply.text, isNot(contains('POST /v1/work-runs')));
    expect(reply.text, contains(_runId));
  });

  test('work-run fields survive a ChatStore round trip', () {
    const m = ChatMessage(
      role: Role.assistant,
      content: 'x',
      responseMetadata:
          ChatResponseMetadata(workRunId: _runId, workStatus: 'running'),
    );
    final back = ChatMessage.fromJson(
        jsonDecode(jsonEncode(m.toJson())) as Map<String, dynamic>);
    expect(back.responseMetadata!.workRunId, _runId);
    expect(back.responseMetadata!.workRunning, isTrue);
    final done = back.responseMetadata!.withWork(workStatus: 'returned');
    expect(done.workRunning, isFalse);
    expect(done.workRunId, _runId);
  });

  test('a malformed work_run_id is dropped', () {
    final m = ChatResponseMetadata.fromJson(
        {'work_run_id': '../../etc', 'work_status': 'running'});
    expect(m.workRunId, isEmpty);
    expect(m.workRunning, isFalse);
  });

  test('Refresh: running, then returned with the persisted answer', () async {
    var calls = 0;
    final seen = <http.Request>[];
    await http.runWithClient(() async {
      final runs = SonderApi(baseUrl: 'http://127.0.0.1:11435').workRuns;
      final first = await runs.get(_runId);
      expect(first.isRunning, isTrue);
      expect(first.hasAnswer, isFalse);
      expect(first.budget, const Duration(minutes: 30));
      final second = await runs.get(_runId);
      expect(second.hasAnswer, isTrue);
      expect(
          second.output, 'The PSO cache now warms on load; 3 files changed.');
    },
        () => MockClient((r) async {
              seen.add(r);
              calls++;
              return fixtureResponse(
                  calls == 1
                      ? 'work_run_running.json'
                      : 'work_run_returned.json',
                  200);
            }));
    expect(seen.map((r) => '${r.method} ${r.url.path}'),
        ['GET /v1/work-runs/$_runId', 'GET /v1/work-runs/$_runId']);
  });

  test('cancel POSTs once to /cancel', () async {
    final seen = <http.Request>[];
    final run = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
            .workRuns
            .cancel(_runId),
        () => MockClient((r) async {
              seen.add(r);
              return fixtureResponse('work_run_cancelled.json', 200);
            }));
    expect(seen, hasLength(1));
    expect(seen.single.method, 'POST');
    expect(seen.single.url.path, '/v1/work-runs/$_runId/cancel');
    expect(seen.single.headers['Authorization'], 'Bearer k');
    expect(run.cancelRequested, isTrue);
  });

  test('list parses the runs', () async {
    final runs = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435').workRuns.list(),
        () => MockClient((r) async => http.Response(
            jsonEncode({
              'runs': [
                {
                  ...jsonDecode(serverFixture('work_run_running.json'))
                      as Map<String, dynamic>
                }..remove('output'),
              ]
            }),
            200)));
    expect(runs.single.id, _runId);
    expect(runs.single.isRunning, isTrue);
    final empty = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435').workRuns.list(),
        () => MockClient(
            (r) async => fixtureResponse('work_runs_empty.json', 200)));
    expect(empty, isEmpty);
  });

  test('403 reads "Work runs need a developer or admin account"', () async {
    await expectLater(
      http.runWithClient(
          () =>
              SonderApi(baseUrl: 'http://127.0.0.1:11435').workRuns.get(_runId),
          () => MockClient((r) async =>
              fixtureResponse('work_runs_forbidden_403.json', 403))),
      throwsA(isA<SonderException>()
          .having((e) => e.message, 'message',
              'Work runs need a developer or admin account.')
          .having((e) => e.code, 'code', 'FORBIDDEN')),
    );
  });

  test('429 WORK_CAPACITY_EXHAUSTED on a turn is a readable notice', () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435').chatDetailed(_hi),
          () => MockClient((r) async => fixtureResponse(
              'work_capacity_exhausted_429.json', 429,
              headers: {...jsonHeaders, 'retry-after': '1'}))),
      throwsA(isA<SonderException>()
          .having((e) => e.code, 'code', 'WORK_CAPACITY_EXHAUSTED')
          .having(
              (e) => e.message, 'message', contains('Stop a running work run'))
          .having((e) => e.message, 'message', isNot(contains('POST /v1')))),
    );
  });

  test('an invalid id is refused without a request', () async {
    var requests = 0;
    await http.runWithClient(() async {
      final runs = SonderApi(baseUrl: 'http://127.0.0.1:11435').workRuns;
      await expectLater(runs.get('../admin'), throwsA(isA<SonderException>()));
      await expectLater(runs.cancel('wr-XYZ'), throwsA(isA<SonderException>()));
    },
        () => MockClient((r) async {
              requests++;
              return http.Response('{}', 200);
            }));
    expect(requests, 0);
  });

  test('refresh backoff is 2 -> 5 -> 15 s', () {
    expect([0, 1, 2, 9].map(workRunPollDelay).map((d) => d.inSeconds),
        [2, 5, 15, 15]);
  });
}
