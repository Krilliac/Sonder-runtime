import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/agent_lanes.dart';
import 'package:sonder_runtime/agents/agent_status.dart';
import 'package:sonder_runtime/agents/transcript_model.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';

AgentEvent _event(int sequence, String type, Map<String, dynamic> payload,
        {String? at}) =>
    AgentEvent.fromJson({
      'sequence': sequence,
      'event_id': 'ev-$sequence',
      'event_type': type,
      'payload': payload,
      if (at != null) 'occurred_at': at,
    });

void main() {
  group('transcript', () {
    test('tool-request responses become tool cards, never prose', () {
      final items = buildTranscript([
        _event(1, 'model.response', {'content': 'Reading the parser first.'}),
        _event(2, 'model.response', {
          'content': jsonEncode({
            'tool': 'read_file',
            'arguments': {'path': 'src/parser.dart'}
          })
        }),
        _event(3, 'tool.requested', {
          'name': 'read_file',
          'call_id': 'call-1',
          'arguments': {'path': 'src/parser.dart'},
        }),
        _event(4, 'tool.result', {
          'name': 'read_file',
          'call_id': 'call-1',
          'success': true,
          'output': 'void parse() {}',
        }),
      ], const [], active: true);
      expect(items.whereType<MessageItem>().map((m) => m.content),
          ['Reading the parser first.']);
      final tool = items.whereType<ToolItem>().single;
      expect(tool.name, 'read_file');
      expect(tool.state, ToolState.done);
      expect(tool.salientArgument, 'src/parser.dart');
      expect(tool.output, 'void parse() {}');
      expect(tool.duration, isNull, reason: 'no measured time, none shown');
    });

    test('a call without a result is requested only while the lane is active',
        () {
      final events = [
        _event(1, 'tool.requested', {
          'name': 'run_tests',
          'call_id': 'call-2',
          'arguments': {'suite': 'render'},
        }),
      ];
      expect(buildTranscript(events, const [], active: true).single,
          isA<ToolItem>().having((t) => t.state, 'state', ToolState.requested));
      expect(buildTranscript(events, const [], active: false).single,
          isA<ToolItem>().having((t) => t.state, 'state', ToolState.noResult));
    });

    test('failures, rejections and measured durations come from the server',
        () {
      final items = buildTranscript([
        _event(1, 'tool.requested',
            {'name': 'edit_file', 'call_id': 'c1', 'arguments': {}},
            at: '2026-10-01T10:00:00Z'),
        _event(
            2,
            'tool.result',
            {
              'name': 'edit_file',
              'call_id': 'c1',
              'success': false,
              'error_code': 'TEXT_NOT_FOUND',
            },
            at: '2026-10-01T10:00:01.500Z'),
        _event(3, 'model.response', {
          'content': jsonEncode({
            'tool': 'shell',
            'arguments': {'cmd': 'ls'}
          })
        }),
        _event(4, 'tool.rejected',
            {'error_code': 'TOOL_REQUEST_REJECTED', 'source_sequence': 3}),
      ], const [], active: false);
      final tools = items.whereType<ToolItem>().toList();
      expect(tools.first.state, ToolState.failed);
      expect(tools.first.errorCode, 'TEXT_NOT_FOUND');
      expect(tools.first.duration, const Duration(milliseconds: 1500));
      expect(tools.last.state, ToolState.rejected);
      expect(tools.last.name, 'shell');
      expect(tools.last.arguments, {'cmd': 'ls'});
      expect(items.whereType<MessageItem>(), isEmpty);
    });

    test('lifecycle markers mark resumes and stops, not the first start', () {
      final items = buildTranscript([
        _event(1, 'lane.running', {}),
        _event(2, 'lane.stopped', {'status': 'interrupted'}),
        _event(3, 'lane.running', {}),
        _event(4, 'lane.failed',
            {'status': 'awaiting_input', 'error': 'BUDGET_EXHAUSTED'}),
      ], const [], active: false);
      expect(items.whereType<LifecycleItem>().map((i) => i.kind), [
        LifecycleKind.interrupted,
        LifecycleKind.resumed,
        LifecycleKind.stalled,
      ]);
      expect((items.last as LifecycleItem).detail, 'BUDGET_EXHAUSTED');
    });

    test('delivery state comes from the snapshot message when present', () {
      final items = buildTranscript([
        _event(5, 'lane.message', {'author': 'user', 'content': 'Also Vulkan'}),
      ], [
        AgentMessage.fromJson({
          'sequence': 5,
          'author': 'user',
          'content': 'Also Vulkan',
          'delivery_state': 'queued',
        }),
      ], active: true);
      final message = items.single as MessageItem;
      expect(message.deliveryState, 'queued');
      expect(message.authorLabel, 'You');
    });
  });

  test('server timestamps are read only when the server sends them', () {
    expect(AgentLane.fromJson({'id': 'a'}).updatedAt, isNull);
    expect(serverTime(0), isNull);
    expect(serverTime(''), isNull);
    expect(serverTime(1790000000)!.isUtc, isTrue);
    expect(serverTime('2026-10-01T10:00:00Z'), DateTime.utc(2026, 10, 1, 10));
    expect(AgentLane.fromJson({'id': 'a', 'updated_ts': 1790000000}).updatedAt,
        isNotNull);
  });

  test('every lane status shows a glyph and its own word', () {
    String shown(String status) {
      final lane = AgentLane.fromJson({'id': 'a', 'status': status});
      return '${lane.statusKind.glyph} ${lane.statusLabel}';
    }

    expect(shown('running'), '◈ Running');
    // A request is never drawn as its acknowledgement.
    expect(shown('interrupt_requested'), '◈ Interrupt requested');
    expect(shown('interrupted'), '! Interrupted');
    expect(shown('cancel_requested'), '◈ Cancel requested');
    expect(shown('cancelled'), '– Cancelled');
    expect(shown('awaiting_input'), '! Needs input');
    expect(shown('failed'), '✗ Failed');
    expect(shown('completed'), '✓ Completed');
    expect(AgentLane.fromJson({'id': 'a', 'status': 'queued'}).statusKind,
        StatusKind.note);
    expect(
        AgentLane.fromJson({'id': 'a', 'status': 'interrupted'})
            .matches(AgentFilter.attention),
        isTrue);
    expect(
        AgentLane.fromJson({'id': 'a', 'status': 'cancel_requested'})
            .matches(AgentFilter.working),
        isTrue);
  });

  // The fixture server is plain HTTP off this device: the person has
  // explicitly allowed it to receive the key (see cleartext_key_test.dart).
  setUpAll(() => CleartextKeyPolicy.allowOnly(['test:80']));
  tearDownAll(() => CleartextKeyPolicy.allowOnly(const []));

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
