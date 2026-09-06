import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/agent_lanes.dart';

void main() {
  test('authorization failure never falls back to a different server',
      () async {
    var requests = 0;
    await expectLater(
        http.runWithClient(
            () => SonderApi(baseUrl: 'http://private', apiKey: 'expired')
                .agentLanes(),
            () => MockClient((request) async {
                  requests++;
                  expect(request.url.host, 'private');
                  return http.Response(
                      '{"error":{"message":"Expired key"}}', 401);
                })),
        throwsA(
            isA<SonderException>().having((e) => e.httpStatus, 'status', 401)));
    expect(requests, 1);
  });
  test('lane states preserve requested versus acknowledged interruption', () {
    final requested = AgentLane.fromJson({
      'id': 'a',
      'status': 'interrupt_requested',
    });
    expect(requested.statusLabel, 'Interrupt requested');
    expect(requested.canResume, isFalse);
    expect(
      AgentLane.fromJson({'id': 'a', 'status': 'interrupted'}).canResume,
      isTrue,
    );
  });

  test('execution summary uses only server-owned public lane fields', () {
    final lane = AgentLane.fromJson({
      'id': 'a',
      'status': 'running',
      'tier': 'code',
      'revision': 7,
      'max_steps': 8,
      'used_steps': 2,
    });
    expect(lane.executionSummary, 'Running · tier code · revision 7');
    expect(
      AgentLane.fromJson({'id': 'a', 'status': 'queued'}).executionSummary,
      'Queued · tier unavailable · revision 0',
    );
  });

  test(
    'lane client uses configured bearer and bounded cursor request',
    () async {
      final client = MockClient((request) async {
        expect(request.headers['authorization'], 'Bearer private-key');
        expect(request.url.path, '/v1/agent-lanes');
        expect(request.url.queryParameters['cursor'], '12');
        return http.Response(
          jsonEncode({
            'lanes': [
              {'id': 'child', 'status': 'running'},
            ],
            'next_cursor': 13,
          }),
          200,
        );
      });
      final page = await http.runWithClient(
        () => SonderApi(
          baseUrl: 'http://test',
          apiKey: 'private-key',
        ).agentLanes(cursor: 12),
        () => client,
      );
      expect(page.lanes.single.id, 'child');
      expect(page.nextCursor, 13);
    },
  );

  test(
    'user followup retains command identity and never supplies author',
    () async {
      final client = MockClient((request) async {
        expect(request.url.path, '/v1/agent-lanes/child/messages');
        expect(jsonDecode(request.body), {
          'command_id': 'same-command',
          'content': 'Keep the edge case',
        });
        return http.Response(
          jsonEncode({
            'command_id': 'same-command',
            'revision': 2,
            'lane': {'id': 'child', 'status': 'running'},
          }),
          200,
        );
      });
      final receipt = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://test').agentCommand(
          'child',
          'messages',
          commandId: 'same-command',
          content: 'Keep the edge case',
        ),
        () => client,
      );
      expect(receipt.commandId, 'same-command');
      expect(receipt.lane!.status, 'running');
    },
  );
}
