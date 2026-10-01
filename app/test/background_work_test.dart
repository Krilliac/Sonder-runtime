import 'package:flutter_test/flutter_test.dart';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:sonder_runtime/background_work.dart';
import 'package:sonder_runtime/chat/commands.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/api.dart';

void main() {
  for (final kind in ['fleet', 'autopilot']) {
    test('$kind cancellation uses the existing control command', () async {
      final requests = <http.Request>[];
      await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:1').cancelBackground(kind, '$kind-1', project: 'project'),
        () => MockClient((request) async {
          requests.add(request);
          expect(request.url.path, '/v1/chat/completions');
          final body = jsonDecode(request.body) as Map<String, dynamic>;
          expect(body['model'], 'sonder');
          expect(body['project'], 'project');
          expect(body['messages'].last['content'], kind == 'fleet' ? '/agentcancel fleet-1' : '/autopilot cancel autopilot-1');
          return http.Response(jsonEncode({'choices': [{'message': {'content': 'cancellation requested'}}]}), 200);
        }),
      );
      expect(requests, hasLength(1));
    });
  }
  test('a refused cancellation is not success', () async {
    await expectLater(http.runWithClient(
      () => SonderApi(baseUrl: 'http://127.0.0.1:1').cancelBackground('fleet', 'master-1'),
      () => MockClient((_) async => http.Response(jsonEncode({'choices': [{'message': {'content': 'ERROR: not allowed'}}]}), 200)),
    ), throwsA(isA<SonderException>()));
  });
  test('choice commands preserve long tasks and omit oversize actions', () {
    final command = '/master_orchestrate fleet 0 ${'task ' * 600}';
    expect(OrchestrationChoice.fromJson({'label': 'Fleet', 'command': command}).command, command);
    expect(OrchestrationChoice.fromJson({'label': 'Fleet', 'command': 'x' * 32769}).command, isEmpty);
  });
  test('background work keeps all groups and fleet children', () {
    final work = BackgroundWork.fromJson({
      'groups': {
        'lanes': [
          {'id': 'lane-1', 'task': 'lane task', 'status': 'running'},
        ],
        'fleets': [
          {
            'id': 'fleet-1',
            'task': 'fleet task',
            'status': 'running',
            'requested_agents': 3,
            'worker_slots': 2,
            'counts': {'done': 1, 'running': 1, 'queued': 1},
            'children': [
              {'id': 'child-1', 'status': 'done', 'preview': 'finished'},
            ],
          },
        ],
        'autopilot': [
          {
            'id': 'auto-1',
            'objective': 'objective',
            'status': 'running',
            'phase': 'executing',
            'current_task': 'task 2',
            'task_counts': {'total': 3, 'done': 1, 'running': 1},
          },
        ],
      },
    });

    expect(work.lanes.single.id, 'lane-1');
    expect(work.fleets.single.countSummary, '1 done · 1 running · 1 queued');
    expect(work.fleets.single.children.single.preview, 'finished');
    expect(work.autopilot.single.taskCountSummary, '1/3 tasks');
  });

  test('background metadata is bounded and round trips', () {
    final metadata = ChatResponseMetadata.fromJson({
      'agent_lane': {
        'lane_id': 'lane-1',
        'folder': 'C:/state/creations/lane-1',
        'status': 'running',
      },
      'orchestration': {
        'task': 'make something',
        'worker_slots': 2,
        'choices': [
          {'label': 'Inline', 'command': '/master inline make something'},
          {'label': 'Fleet', 'command': '/master fleet 0 make something'},
        ],
      },
    });

    expect(metadata.agentLane!.laneId, 'lane-1');
    expect(metadata.orchestration!.choices, hasLength(2));
    expect(metadata.toJson()['agent_lane'], isA<Map<String, Object>>());
  });

  test('offline palette exposes delegation and master orchestrate', () {
    expect(quickCommands['/delegate'], isNotEmpty);
    expect(quickCommands['/master_orchestrate'], isNotEmpty);
  });
}
