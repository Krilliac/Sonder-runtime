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

  test('403 reads "Approvals need a developer or admin account"', () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .approvals
              .approve(_call),
          () => MockClient((r) async =>
              fixtureResponse('permission_mode_forbidden_403.json', 403))),
      throwsA(isA<SonderException>().having((e) => e.message, 'message',
          'Approvals need a developer or admin account.')),
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
      expect(r!.callId, _call);
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
