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
}
