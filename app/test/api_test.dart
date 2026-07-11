import 'package:flutter_test/flutter_test.dart';
import 'package:trilobite/api.dart';

void main() {
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
