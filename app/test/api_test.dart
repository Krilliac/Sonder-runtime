import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:trilobite/api.dart';
import 'package:trilobite/models.dart';

void main() {
  test('location opt-in sends a minimized client-side place hint', () async {
    Map<String, dynamic>? chatBody;
    final client = MockClient((request) async {
      if (request.url.host == 'ipwho.is') {
        return http.Response(
            jsonEncode({
              'success': true,
              'ip': '203.0.113.77',
              'city': 'Chicago',
              'region': 'Illinois',
              'country': 'United States',
              'country_code': 'US',
              'latitude': 41.8,
              'longitude': -87.6,
              'timezone': {
                'id': 'America/Chicago',
                'abbr': 'CDT',
                'offset': -18000,
              },
            }),
            200);
      }
      chatBody = jsonDecode(request.body) as Map<String, dynamic>;
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'weather live'}
              }
            ]
          }),
          200);
    });

    final output = await http.runWithClient(
      () => const TrilobiteApi(baseUrl: 'http://trilobite.test').chat(
        const [ChatMessage(role: Role.user, content: 'weather in my area')],
        allowApproximateLocation: true,
      ),
      () => client,
    );

    expect(output, 'weather live');
    expect(chatBody?['location_consent'], isTrue);
    final hint = chatBody?['location_hint'] as Map<String, dynamic>;
    expect(hint['city'], 'Chicago');
    expect(hint.containsKey('ip'), isFalse);
    expect(hint.containsKey('latitude'), isFalse);
    expect(hint.containsKey('longitude'), isFalse);
    expect(hint['timezone'], 'America/Chicago');
  });

  test('explicit weather city does not perform an IP location lookup',
      () async {
    var locationRequests = 0;
    Map<String, dynamic>? chatBody;
    final client = MockClient((request) async {
      if (request.url.host == 'ipwho.is') {
        locationRequests += 1;
        return http.Response('{}', 200);
      }
      chatBody = jsonDecode(request.body) as Map<String, dynamic>;
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'Tokyo weather'}
              }
            ]
          }),
          200);
    });

    final output = await http.runWithClient(
      () => const TrilobiteApi(baseUrl: 'http://trilobite.test').chat(
        const [ChatMessage(role: Role.user, content: 'weather in Tokyo')],
        allowApproximateLocation: true,
      ),
      () => client,
    );

    expect(output, 'Tokyo weather');
    expect(locationRequests, 0);
    expect(chatBody?['location_consent'], isTrue);
    expect(chatBody?.containsKey('location_hint'), isFalse);
  });

  test('activity response preserves exact actions and checklist state', () {
    final status = ActivityStatus.fromJson({
      'active_count': 0,
      'total_tool_calls': 9,
      'latest': {
        'id': 'r000123',
        'label': 'agent:code',
        'status': 'complete',
        'elapsed_ms': 420,
        'tool_calls': 2,
        'model_calls': 1,
        'result_summary': 'Created and verified the script.',
        'events': [
          {
            'kind': 'tool_call',
            'tool': 'image_inspect',
            'title': 'Viewed Image',
            'command': 'image_inspect frame.png',
            'output': 'PNG 640x360',
            'elapsed_ms': 12,
            'ok': true,
          },
        ],
        'checklist': {
          'title': 'Build smoke asset',
          'status': 'done',
          'items': [
            {'id': 'a', 'title': 'Inspect files', 'status': 'done'},
            {'id': 'b', 'title': 'Run validation', 'status': 'done'},
          ],
        },
      },
    });

    final response = status.displayResponse!;
    expect(status.totalToolCalls, 9);
    expect(response.resultSummary, 'Created and verified the script.');
    expect(response.actions, hasLength(1));
    expect(response.actions.single.title, 'Viewed Image');
    expect(response.actions.single.evidence, contains('PNG 640x360'));
    expect(response.checklistTitle, 'Build smoke asset');
    expect(response.checklist.map((item) => item.status), everyElement('done'));
  });

  test('agent status preserves scheduler capacity and cancellation state', () {
    final status = AgentStatus.fromJson({
      'active_agents': 12,
      'cancel_pending': 2,
      'interrupted_agents': 4,
      'total_agents': 33,
      'total_listed': 20,
      'tokens_in': 100,
      'tokens_out': 50,
      'agents': const [],
      'events': const [],
      'capacity': {
        'logical_cpus': 16,
        'agent_ceiling': 32,
        'worker_slots': 2,
        'automatic_worker_slots': 2,
        'total_memory_bytes': 17179869184,
        'available_memory_bytes': 4294967296,
        'source': 'auto',
      },
    });

    expect(status.activeAgents, 12);
    expect(status.cancelPending, 2);
    expect(status.interruptedAgents, 4);
    expect(status.totalAgents, 33);
    expect(status.capacity?.agentCeiling, 32);
    expect(status.capacity?.workerSlots, 2);
    expect(status.capacity?.availableMemoryBytes, 4294967296);
  });

  test('agent status falls back to listed count for an older server', () {
    final status = AgentStatus.fromJson({
      'active_agents': 1,
      'total_listed': 7,
      'tokens_in': 0,
      'tokens_out': 0,
      'agents': const [],
      'events': const [],
    });

    expect(status.totalAgents, 7);
    expect(status.cancelPending, 0);
    expect(status.interruptedAgents, 0);
    expect(status.capacity, isNull);
  });
}
