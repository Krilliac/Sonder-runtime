// P1-2 (API part): HTTP approvals with the console fallback when the server
// has no approvals route, and refusal metadata (receipt or text fallback).
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

const _call = '3f9a12c0';

void main() {
  test('a server without approvals (404): list is null, approve says console',
      () async {
    await http.runWithClient(() async {
      final approvals = SonderApi(baseUrl: 'http://127.0.0.1:11435').approvals;
      expect(await approvals.list(), isNull);
      await expectLater(
        approvals.approve(_call),
        throwsA(isA<SonderException>()
            .having((e) => e.code, 'code', ApprovalsApi.unavailableCode)
            .having((e) => e.message, 'message',
                'Approve from the console: `/approve 3f9a12c0`')),
      );
    },
        () => MockClient(
            (r) async => http.Response('{"error":"not_found"}', 404)));
  });

  test('approve sends one POST with ttl and an Idempotency-Key', () async {
    final seen = <http.Request>[];
    final issued = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
            .approvals
            .approve(_call, ttl: const Duration(minutes: 15)),
        () => MockClient((r) async {
              seen.add(r);
              return http.Response(
                  jsonEncode({
                    'approval': {
                      'nonce': 'n_c41a',
                      'call_id': _call,
                      'tool': 'write_file',
                      'ttl_seconds': 900,
                    }
                  }),
                  200);
            }));
    expect(seen, hasLength(1));
    expect(seen.single.url.path, '/v1/approvals/$_call');
    expect(jsonDecode(seen.single.body), {'ttl_seconds': 900});
    expect(seen.single.headers['Idempotency-Key'], startsWith('approve-'));
    expect(issued.nonce, 'n_c41a');
    expect(issued.tool, 'write_file');
    expect(issued.ttlSeconds, 900);
  });

  // The server lets developers list approvals but issues and revokes them
  // for an administrator (or the deployment API key) only, so a 403 on the
  // POST routes must not tell a developer to sign in as a developer.
  const developerApprove403 = '{"error":{"message":"administrator '
      'authorization is required to approve or revoke calls",'
      '"type":"forbidden","code":"FORBIDDEN"}}';

  test('403 on approve says an administrator or the API key is needed',
      () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .approvals
              .approve(_call),
          () => MockClient((r) async =>
              fixtureResponse('permission_mode_forbidden_403.json', 403))),
      throwsA(isA<SonderException>()
          .having((e) => e.message, 'message', ApprovalsApi.forbiddenToIssue)
          .having((e) => e.remedy, 'remedy', contains('administrator'))),
    );
  });

  test('a developer approving gets 403: the wording names the administrator',
      () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .approvals
              .approve(_call),
          () => MockClient(
              (r) async => http.Response(developerApprove403, 403))),
      throwsA(isA<SonderException>()
          .having((e) => e.httpStatus, 'httpStatus', 403)
          .having((e) => e.message, 'message',
              'Approving or revoking needs an administrator account or the '
                  'deployment API key.')
          .having((e) => e.message, 'message',
              isNot(contains('developer or admin')))
          .having((e) => e.remedy, 'remedy',
              isNot(contains('developer or admin')))),
    );
  });

  test('a developer revoking gets 403: the wording names the administrator',
      () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .approvals
              .revoke('n_c41a'),
          () => MockClient(
              (r) async => http.Response(developerApprove403, 403))),
      throwsA(isA<SonderException>()
          .having((e) => e.httpStatus, 'httpStatus', 403)
          .having(
              (e) => e.message, 'message', ApprovalsApi.forbiddenToIssue)),
    );
  });

  test('403 on the list still reads "Approvals need a developer or admin '
      'account"', () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435').approvals.list(),
          () => MockClient((r) async => http.Response(
              '{"error":{"message":"developer or administrator authorization '
              'is required to review or approve calls","type":"forbidden",'
              '"code":"FORBIDDEN"}}',
              403))),
      throwsA(isA<SonderException>()
          .having((e) => e.message, 'message',
              'Approvals need a developer or admin account.')
          .having((e) => e.remedy, 'remedy',
              'Sign in with a developer or admin account.')),
    );
  });

  test('list parses pending calls and open approvals', () async {
    final snap = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435').approvals.list(),
        () => MockClient((r) async => http.Response(
            jsonEncode({
              'pending': [
                {
                  'call_id': _call,
                  'tool': 'write_file',
                  'preview': 'path=src/render/pso_cache.cpp',
                  'mode': 'manual',
                  'refused_at': 1790335140,
                },
                {'tool': 'no id, dropped'},
              ],
              'approvals': [
                {'nonce': 'n_c41a', 'call_id': _call, 'expires_at': 1790336040}
              ],
            }),
            200)));
    expect(snap!.pending.single.callId, _call);
    expect(snap.pending.single.mode, 'manual');
    expect(snap.open.single.nonce, 'n_c41a');
    expect(snap.open.single.expiresAt, isNotNull);
  });

  test('revoke posts to /revoke/<nonce>; bad ids send nothing', () async {
    final seen = <String>[];
    await http.runWithClient(() async {
      final approvals = SonderApi(baseUrl: 'http://127.0.0.1:11435').approvals;
      await approvals.revoke('n_c41a');
      await expectLater(
          approvals.approve('XYZ'), throwsA(isA<SonderException>()));
      await expectLater(
          approvals.revoke('a/b'), throwsA(isA<SonderException>()));
    },
        () => MockClient((r) async {
              seen.add('${r.method} ${r.url.path}');
              return http.Response('{"ok":true}', 200);
            }));
    expect(seen, ['POST /v1/approvals/revoke/n_c41a']);
  });

  test('deny posts to /deny/<call_id> with Idempotency-Key and tool/digest',
      () async {
    final seen = <http.Request>[];
    final digest = 'b' * 64;
    await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
            .approvals
            .deny(_call, tool: 'run_code', digest: digest),
        () => MockClient((r) async {
              seen.add(r);
              return http.Response(
                  jsonEncode({
                    'object': 'denial',
                    'denial': {
                      'tool': 'run_code',
                      'call_id': _call,
                      'digest': digest,
                    },
                  }),
                  200);
            }));
    expect(seen, hasLength(1));
    expect(seen.single.url.path, '/v1/approvals/deny/$_call');
    expect(seen.single.headers['Idempotency-Key'], startsWith('deny-'));
    expect(jsonDecode(seen.single.body), {'tool': 'run_code', 'digest': digest});
  });

  test('PendingApproval parses detail, denied and bidi_warning fields',
      () async {
    final snap = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435').approvals.list(),
        () => MockClient((r) async => http.Response(
            jsonEncode({
              'pending': [
                {
                  'call_id': _call,
                  'tool': 'run_code',
                  'preview': 'language=python',
                  'mode': 'manual',
                  'detail': 'print(1)',
                  'detail_hidden': false,
                  'detail_truncated': true,
                  'detail_chars': 5000,
                  'denied': true,
                  'bidi_warning': true,
                },
              ],
            }),
            200)));
    final p = snap!.pending.single;
    expect(p.detail, 'print(1)');
    expect(p.detailTruncated, isTrue);
    expect(p.detailChars, 5000);
    expect(p.denied, isTrue);
    expect(p.bidiWarning, isTrue);
  });

  group('refusal metadata', () {
    test('structured sonder_receipt.refusal (server S1)', () async {
      final reply = await http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435').chatDetailed(
              const [ChatMessage(role: Role.user, content: '/write x')]),
          () => MockClient((r) async => http.Response(
              jsonEncode({
                'choices': [
                  {
                    'message': {'content': 'refused write_file'}
                  }
                ],
                'sonder_receipt': {
                  'refusal': {
                    'kind': 'refused',
                    'tool': 'write_file',
                    'call_id': _call,
                    'reason': 'manual mode asks first',
                    'remedies': ['/approve $_call', '/mode acceptEdits'],
                  }
                }
              }),
              200)));
      final refusal = reply.refusal!;
      expect(refusal.callId, _call);
      expect(refusal.tool, 'write_file');
      expect(refusal.remedies, ['/approve $_call', '/mode acceptEdits']);
      final back = ChatResponseMetadata.fromJson(reply.metadata!.toJson());
      expect(back.refusal!.callId, _call);
    });

    test('text fallback until S1', () {
      final r = ChatRefusal.fromText(
          'refused /write notes.txt: file changes need a person to confirm.\n'
          'Approve once: /approve 3f9a12c0');
      // Model-authored text never carries an approvable call id.
      expect(r!.callId, isEmpty);
      expect(r.remedies, isEmpty);
      expect(r.reason, startsWith('refused /write notes.txt'));
      final colon = ChatRefusal.fromText(
          'refused: raising the permission mode from manual to auto needs a '
          'person to confirm it');
      expect(colon!.callId, isEmpty);
      expect(ChatRefusal.fromText('The refused call was retried.'), isNull);
      expect(ChatRefusal.fromText('ok'), isNull);
      expect(
          ChatRefusal.fromText('Refused connections usually mean the port '
              'is closed.'),
          isNull);
    });
  });
}
