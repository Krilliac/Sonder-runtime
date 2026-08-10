import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

void main() {
  test('host launcher status uses its independent bearer token', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(
        jsonEncode({
          'ok': true,
          'launcher': 'ready',
          'server_running': false,
          'server_host': '0.0.0.0',
          'server_port': 11435,
          'last_action': '',
          'last_error': '',
        }),
        200,
      );
    });

    final status = await http.runWithClient(
      () => const SonderLauncherApi(
        baseUrl: 'https://host.test:11436/',
        token: 'launcher-secret',
      ).status(),
      () => client,
    );

    expect(seen.url.toString(),
        'https://host.test:11436/v1/launcher/status');
    expect(seen.headers['authorization'], 'Bearer launcher-secret');
    expect(status.launcher, 'ready');
    expect(status.serverRunning, isFalse);
    expect(status.serverState, 'stopped');
  });

  test('host launcher sends only a bounded action and context size', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(
        jsonEncode({
          'ok': true,
          'launcher': 'ready',
          'server_running': true,
          'server_host': '0.0.0.0',
          'server_port': 11435,
          'last_action': 'start',
          'last_error': '',
          'message': 'started',
        }),
        200,
      );
    });

    final status = await http.runWithClient(
      () => const SonderLauncherApi(
        baseUrl: 'https://host.test:11436',
        token: 'secret',
      ).action('start', contextSize: '32k'),
      () => client,
    );

    expect(seen.url.path, '/v1/launcher/start');
    expect(jsonDecode(seen.body), {'context_size': '32k'});
    expect(
      seen.headers['idempotency-key'],
      matches(RegExp(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
        r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
      )),
    );
    expect(status.serverRunning, isTrue);
    expect(status.message, 'started');
    expect(
      const SonderLauncherApi(baseUrl: 'x', token: '').action('run'),
      throwsA(isA<SonderException>()),
    );
  });

  test('host launcher follows an accepted async operation to success',
      () async {
    final requests = <http.Request>[];
    var operationReads = 0;
    Map<String, dynamic> payload(String phase) => {
          'ok': phase != 'failed',
          'launcher': 'ready',
          'server_running': phase == 'succeeded',
          'server_host': '0.0.0.0',
          'server_port': 11435,
          'last_action': 'start',
          'last_error': '',
          'operation_id': 'op-12345678',
          'operation_phase': phase,
          'operation': {
            'id': 'op-12345678',
            'action': 'start',
            'phase': phase,
            'message': phase == 'succeeded' ? 'Server started.' : 'Working.',
            'last_error': '',
          },
        };
    final client = MockClient((request) async {
      requests.add(request);
      if (request.method == 'POST') {
        return http.Response(jsonEncode(payload('queued')), 202);
      }
      operationReads += 1;
      if (operationReads == 1) {
        return http.Response(jsonEncode({'error': 'temporarily unavailable'}),
            503);
      }
      return http.Response(
        jsonEncode(payload(operationReads == 2 ? 'running' : 'succeeded')),
        200,
      );
    });
    final phases = <String>[];

    final result = await http.runWithClient(
      () => const SonderLauncherApi(
        baseUrl: 'https://host.test:11436',
        token: 'secret',
      ).action(
        'start',
        idempotencyKey: 'tap-key-12345678',
        maxWait: const Duration(seconds: 1),
        pollInterval: Duration.zero,
        onProgress: (status) {
          phases.add(status.currentOperation!.phase);
        },
      ),
      () => client,
    );

    expect(result.serverRunning, isTrue);
    expect(phases, ['queued', 'running', 'succeeded']);
    expect(requests, hasLength(4));
    expect(requests.first.headers['idempotency-key'], 'tap-key-12345678');
    expect(
      requests.last.url.path,
      '/v1/launcher/operations/op-12345678',
    );
  });

  test('host launcher reports terminal async failures without another POST',
      () async {
    var posts = 0;
    final client = MockClient((request) async {
      if (request.method == 'POST') {
        posts += 1;
        return http.Response(
          jsonEncode({
            'ok': true,
            'launcher': 'ready',
            'operation': {
              'id': 'op-failure',
              'action': 'restart',
              'phase': 'queued',
            },
          }),
          202,
        );
      }
      return http.Response(
        jsonEncode({
          'ok': false,
          'launcher': 'ready',
          'last_error': 'server health check failed',
          'operation': {
            'id': 'op-failure',
            'action': 'restart',
            'phase': 'failed',
            'last_error': 'server health check failed',
          },
        }),
        200,
      );
    });

    await expectLater(
      http.runWithClient(
        () => const SonderLauncherApi(
          baseUrl: 'https://host.test:11436',
          token: 'secret',
        ).action(
          'restart',
          maxWait: const Duration(seconds: 1),
          pollInterval: Duration.zero,
        ),
        () => client,
      ),
      throwsA(
        isA<SonderException>().having(
          (error) => error.message,
          'message',
          contains('health check failed'),
        ),
      ),
    );
    expect(posts, 1);
  });

  test('stopping async wait does not send a second launcher request',
      () async {
    var requests = 0;
    var cancelled = false;
    final client = MockClient((request) async {
      requests += 1;
      return http.Response(
        jsonEncode({
          'ok': true,
          'launcher': 'ready',
          'operation': {
            'id': 'op-cancel-wait',
            'action': 'start',
            'phase': 'queued',
          },
        }),
        202,
      );
    });

    await expectLater(
      http.runWithClient(
        () => const SonderLauncherApi(
          baseUrl: 'https://host.test:11436',
          token: 'secret',
        ).action(
          'start',
          maxWait: const Duration(seconds: 1),
          pollInterval: Duration.zero,
          onProgress: (_) => cancelled = true,
          isCancelled: () => cancelled,
        ),
        () => client,
      ),
      throwsA(
        isA<SonderException>().having(
          (error) => error.message,
          'message',
          contains('may still be running'),
        ),
      ),
    );
    expect(requests, 1);
  });

  test('launcher status exposes a resumable active operation', () async {
    final client = MockClient((request) async => http.Response(
          jsonEncode({
            'ok': true,
            'launcher': 'ready',
            'active_operation': {
              'id': 'op-resume',
              'action': 'start',
              'phase': 'running',
              'message': 'Downloading model.',
            },
          }),
          200,
        ));

    final result = await http.runWithClient(
      () => const SonderLauncherApi(
        baseUrl: 'https://host.test:11436',
        token: 'secret',
      ).status(),
      () => client,
    );

    expect(result.activeOperation?.id, 'op-resume');
    expect(result.currentOperation?.phase, 'running');
    expect(result.currentOperation?.displayMessage, 'Downloading model.');
  });

  test('launcher status distinguishes a foreign listener from Sonder Runtime',
      () {
    final status = LauncherStatus.fromJson({
      'ok': true,
      'launcher': 'ready',
      'server_running': false,
      'server_state': 'foreign_listener',
      'server_host': '0.0.0.0',
      'server_port': 11435,
      'last_error': 'configured port is occupied by another service',
    });

    expect(status.ok, isTrue);
    expect(status.serverRunning, isFalse);
    expect(status.serverState, 'foreign_listener');
    expect(status.lastError, contains('another service'));
  });

  test('Sonder API uses the canonical status namespace', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(jsonEncode({'models': []}), 200);
    });

    await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test').systemInfo(),
      () => client,
    );

    expect(seen.url.path, '/v1/sonder/status');
  });

  test('Sonder account calls use the canonical command namespace', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(
        jsonEncode({'ok': true, 'message': 'registered'}),
        200,
      );
    });

    final result = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test')
          .register('person', 'password123'),
      () => client,
    );

    expect(result, 'registered');
    expect(seen.url.path, '/v1/sonder/register');
  });

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
      () => const SonderApi(baseUrl: 'http://sonder.test').chat(
        const [ChatMessage(role: Role.user, content: 'weather in my area')],
        allowApproximateLocation: true,
      ),
      () => client,
    );

    expect(output, 'weather live');
    expect(chatBody?['model'], 'sonder');
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
      () => const SonderApi(baseUrl: 'http://sonder.test').chat(
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

  test('execution feed is bounded structured and client redacted', () {
    final live = List<Map<String, dynamic>>.generate(
      25,
      (index) => {
        'response_id': 'response-1',
        'response_status': 'running',
        'seq': index,
        'ts': '2026-08-09T12:00:${(index % 60).toString().padLeft(2, '0')}Z',
        'kind': 'other',
        'phase': 'completed',
        'summary_preview': {
          'state': 'available',
          'text': 'event $index',
          'chars': 7,
          'truncated': false,
          'redacted': false,
        },
      },
    );
    live[22] = {
      'response_id': 'response-1',
      'response_status': 'running',
      'seq': 22,
      'ts': '2026-08-09T12:22:00Z',
      'kind': 'model_call',
      'phase': 'completed',
      'model': 'sonder:latest',
      'prompt_chars': 120,
      'history_messages': 3,
      'tokens_in': 40,
      'tokens_out': 20,
      'ok': true,
      'request_preview': {'state': 'disabled'},
      'response_preview': {'state': 'unavailable'},
    };
    live[23] = {
      'response_id': 'response-1',
      'response_status': 'running',
      'seq': 23,
      'ts': '2026-08-09T12:23:00Z',
      'kind': 'tool_call',
      'phase': 'completed',
      'tool': 'file_edit',
      'title': 'Edit file',
      'ok': true,
      'args_preview': {'state': 'disabled'},
      'command_preview': {'state': 'disabled'},
      'result_preview': {'state': 'unavailable'},
    };
    live[24] = {
      'response_id': 'response-1',
      'response_status': 'complete',
      'seq': 25,
      'ts': '2026-08-09T12:34:56Z',
      'kind': 'file_change',
      'phase': 'completed',
      'ok': true,
      'elapsed_ms': 42,
      'action': 'edit',
      'path': r'C:\Users\private-name\repo\harness.dart',
      'lines_added': 8,
      'lines_edited': 1,
      'lines_deleted': 2,
      'bytes': 512,
      'dry_run': false,
      'preview_kind': 'content',
      'content_preview': {
        'state': 'available',
        'text':
            'Authorization: Bearer top-secret-token ${List.filled(400, 'x').join()}',
        'chars': 450,
        'truncated': true,
        'redacted': true,
      },
    };

    final info = SystemInfo.fromJson({
      'execution': {
        'feed': {
          'known': true,
          'schema_version': 1,
          'runtime_id': 'runtime-abc',
          'active_responses': 2,
          'truncated': false,
          'redaction_applied': true,
          'oldest_seq': 5,
          'next_seq': 26,
          'dropped_events': 5,
          'sequence_gap': 2,
          'limits': {'events': 20, 'preview_chars': 1000},
          'error': '',
          'bytes': 4096,
          'events': live,
        },
      },
    });

    final feed = info.executionFeed!;
    expect(feed.events, hasLength(20));
    expect(feed.events.first.summary, 'event 5');
    expect(feed.runtimeId, 'runtime-abc');
    expect(feed.activeResponses, 2);
    expect(feed.eventLimit, 20);
    expect(feed.previewCharLimit, 1000);
    expect(feed.redactionApplied, isTrue);
    expect(feed.oldestSeq, 5);
    expect(feed.nextSeq, 26);
    expect(feed.droppedEvents, 5);
    expect(feed.sequenceGap, 2);
    expect(feed.hasGap, isTrue);
    expect(feed.truncated, isTrue);
    expect(feed.events[17].model, 'sonder:latest');
    expect(feed.events[17].tokensOut, 20);
    expect(feed.events[18].tool, 'file_edit');
    expect(feed.events[18].title, 'Edit file');
    final event = feed.events.last;
    expect(event.timestamp, DateTime.utc(2026, 8, 9, 12, 34, 56));
    expect(event.kind, 'file_change');
    expect(event.fileOperation, 'edit');
    expect(event.status, 'ok');
    expect(event.deltaLabel, 'lines +8 ~1 -2');
    expect(event.path, contains('<user-home>'));
    expect(event.preview, contains('<redacted>'));
    expect(event.preview, isNot(contains('top-secret-token')));
    expect(event.preview.runes.length, lessThanOrEqualTo(300));
    expect(event.displayPreview.redacted, isTrue);
  });

  test('execution feed sanitizes every displayed scalar and preview', () {
    final feed = ExecutionFeed.fromJson({
      'schema_version': 1,
      'runtime_id': 'rt-test',
      'known': true,
      'events': [
        {
          'response_id': 'r\nFAKE',
          'response_status': 'running\u202e',
          'seq': 1,
          'kind': 'future\nkind\u202e',
          'phase': 'done\u2066',
          'model': 'model\u202e',
          'tool': 'tool\x00name',
          'title': 'title\x9b31m',
          'action': 'edit\rdelete',
          'path': 'file\tname.py',
          'preview_kind': 'content\u2069',
          'summary_preview': {
            'state': 'available',
            'text': 'safe\u202ePREVIEW\x1b[31m',
          },
        },
      ],
    });
    final event = feed.events.single;
    final rendered = [
      event.responseId, event.responseStatus, event.kind, event.phase,
      event.model, event.tool, event.title, event.fileOperation, event.path,
      event.previewKind, event.preview,
    ].join('|');
    expect(rendered, isNot(contains(RegExp(
      r'[\x00-\x1F\x7F-\x9F\u202A-\u202E\u2066-\u2069]',
    ))));
    expect(event.kind.length, lessThanOrEqualTo(64));
  });

  test('execution feed is additive and suppresses preview when detail is off', () {
    final oldInfo = SystemInfo.fromJson({
      'activity': {
        'events': ['legacy activity is not an execution feed'],
      },
    });
    final info = SystemInfo.fromJson({
      'execution': {
        'feed': {
          'known': true,
          'schema_version': 1,
          'runtime_id': 'runtime-old',
          'active_responses': null,
          'truncated': false,
          'redaction_applied': false,
          'limits': {'events': 20, 'preview_chars': 1000},
          'error': '',
          'bytes': 100,
          'events': [
            {
              'response_id': 'r1',
              'response_status': 'running',
              'seq': 1,
              'kind': 'unknown-new-kind',
              'phase': 'completed',
              'summary_preview': {
                'state': 'disabled',
                'text': 'must not be exposed',
                'chars': null,
                'truncated': false,
                'redacted': false,
              },
            },
          ],
        },
      },
    });

    expect(oldInfo.executionFeed, isNull);
    expect(info.executionFeed?.known, isTrue);
    expect(info.executionFeed?.activeResponses, isNull);
    expect(info.executionFeed?.schemaVersion, 1);
    expect(info.executionFeed?.events.single.kind, 'unknown-new-kind');
    expect(info.executionFeed?.events.single.preview, isEmpty);
    expect(info.executionFeed?.events.single.previewState, 'disabled');

    final authoritativeNoGap = ExecutionFeed.fromJson({
      'known': true,
      'dropped_events': 0,
      'sequence_gap': 0,
      'events': [
        {'response_id': 'r1', 'seq': 1},
        {'response_id': 'r1', 'seq': 3},
      ],
    });
    expect(authoritativeNoGap.hasGap, isFalse);
    expect(authoritativeNoGap.oldestSeq, isNull);
    expect(authoritativeNoGap.droppedEvents, 0);

    final inferredLegacyGap = ExecutionFeed.fromJson({
      'known': true,
      'events': [
        {'response_id': 'legacy-r1', 'seq': 1},
        {'response_id': 'legacy-r1', 'seq': 3},
      ],
    });
    expect(inferredLegacyGap.sequenceGap, isNull);
    expect(inferredLegacyGap.droppedEvents, isNull);
    expect(inferredLegacyGap.hasGap, isTrue);
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

  test('autopilot status preserves lifecycle budgets tasks and reports', () {
    final status = AutopilotStatus.fromJson({
      'active_runs': 1,
      'resumable_runs': 2,
      'total_runs': 4,
      'total_listed': 4,
      'database': r'C:\state\autopilot.db',
      'latest': {
        'id': 'auto-abc123',
        'objective': 'Implement and validate the feature',
        'project': 'demo',
        'tier': 'code',
        'policy': 'workspace',
        'allow_web': true,
        'status': 'running',
        'phase': 'execute',
        'cycles': 2,
        'failures': 1,
        'checkpoints': 2,
        'replans': 1,
        'max_failures': 3,
        'max_tasks': 12,
        'max_replans': 2,
        'adaptive': true,
        'summary': 'working',
        'final_report': 'autopilot end report',
        'last_error': '',
        'criteria': ['tests pass'],
        'plan': [
          {
            'id': 'task-01',
            'title': 'Inspect',
            'instruction': 'Read the source',
            'kind': 'inspect',
            'status': 'passed',
            'attempts': 1,
            'output': 'done',
            'error': '',
          },
        ],
      },
      'runs': const [],
      'events': [
        {'event_id': 9, 'kind': 'task_pass', 'message': 'task-01 passed'},
      ],
    });

    expect(status.activeRuns, 1);
    expect(status.resumableRuns, 2);
    expect(status.totalRuns, 4);
    expect(status.latest?.id, 'auto-abc123');
    expect(status.latest?.isActive, isTrue);
    expect(status.latest?.adaptive, isTrue);
    expect(status.latest?.checkpoints, 2);
    expect(status.latest?.replans, 1);
    expect(status.latest?.maxReplans, 2);
    expect(status.latest?.tasks.single.status, 'passed');
    expect(status.latest?.criteria, ['tests pass']);
    expect(status.events.single.message, 'task-01 passed');
  });

  test('system info accepts older servers without autopilot state', () {
    final info = SystemInfo.fromJson(const {});
    expect(info.autopilot, isNull);
    expect(info.runtimePolicy, isNull);
    expect(info.mcpRuntime, isNull);
    expect(info.learningHealth, isNull);
    expect(info.execution, isNull);
    expect(info.executionSummary, 'lanes unknown | agents unknown');
    expect(info.models, isEmpty);
  });

  test('system info parses shared live execution counts', () {
    final info = SystemInfo.fromJson({
      'execution': {
        'known': true,
        'running_lanes': 2,
        'running_agents': 3,
        'queued_agents': 4,
        'active_agents': 7,
        'semantics': 'fleet model-call lanes and durable fleet agents',
        'error': '',
      },
    });

    expect(info.execution?.known, isTrue);
    expect(info.execution?.runningLanes, 2);
    expect(info.execution?.runningAgents, 3);
    expect(info.execution?.queuedAgents, 4);
    expect(info.execution?.activeAgents, 7);
    expect(info.executionSummary, 'lanes 2 | agents 3 +4 queued');
  });

  test('unknown execution counts stay nullable rather than becoming zero', () {
    final execution = ExecutionStatus.fromJson({
      'known': false,
      'running_lanes': 0,
      'running_agents': 0,
      'queued_agents': 0,
      'active_agents': 0,
    });

    expect(execution.known, isFalse);
    expect(execution.runningLanes, isNull);
    expect(execution.runningAgents, isNull);
    expect(execution.summary, 'lanes unknown | agents unknown');
  });

  test('system info parses shared local runtime policy state', () {
    final info = SystemInfo.fromJson({
      'runtime_policy': {
        'revision': 7,
        'updated_ts': 1783731000,
        'path': r'C:\Users\example\AppData\Local\sonder\runtime_policy.json',
        'source': 'runtime_policy_update',
        'error': '',
        'local_models': {
          'fast': 'qwen2.5:3b',
          'code': 'sonder:latest',
          'general': 'qwen2.5:7b-instruct',
        },
        'routing': {
          'router': 'fast',
          'workbench': 'code',
          'autopilot': 'code',
          'fleet': 'code',
          'review': 'general',
        },
        'missing_models': ['missing-local:latest'],
      },
    });

    final policy = info.runtimePolicy!;
    expect(policy.revision, 7);
    expect(policy.localModels['code'], 'sonder:latest');
    expect(policy.routing['review'], 'general');
    expect(policy.modelForLane('review'), 'qwen2.5:7b-instruct');
    expect(policy.missingModels, ['missing-local:latest']);
    expect(policy.hasWarning, isTrue);
  });

  test('system info parses live MCP convergence state', () {
    final info = SystemInfo.fromJson({
      'mcp_runtime': {
        'status': 'current',
        'enabled': true,
        'module': '__main__',
        'path': r'C:\sonder\server.py',
        'loaded_digest': '1234567890abcdef',
        'current_digest': '1234567890abcdef',
        'source_changed': false,
        'registered_tools': 108,
        'refresh_count': 3,
        'last_refresh_ts': 1783731000,
        'last_surface_changed': true,
        'last_error': '',
        'last_notification_error': '',
        'protocol_list_changed': true,
      },
    });

    final runtime = info.mcpRuntime!;
    expect(runtime.status, 'current');
    expect(runtime.registeredTools, 108);
    expect(runtime.refreshCount, 3);
    expect(runtime.protocolListChanged, isTrue);
    expect(runtime.loadedShort, '1234567890ab');
    expect(runtime.currentShort, '1234567890ab');
    expect(runtime.hasWarning, isFalse);
  });

  test('system info parses structured learning health', () {
    final info = SystemInfo.fromJson({
      'learning_health': {
        'status': 'healthy',
        'interactions': 4416,
        'outcomes': 3710,
        'outcome_interactions': 3710,
        'good_outcomes': 3596,
        'bad_outcomes': 114,
        'outcome_coverage_percent': 84.0,
        'positive_percent': 96.9,
        'lessons': 974,
        'facts': 8,
        'grounded_lessons': 461,
        'synthetic_lessons': 513,
        'lessons_per_interaction': 0.221,
        'distillation_yield': 0.128,
        'lesson_sources': {'interaction': 461, 'seed': 513},
        'signals': [
          {
            'signal': 'tests_passed',
            'count': 3559,
            'average_reward': 1.0,
            'good': true,
          },
          {
            'signal': 'failed',
            'count': 99,
            'average_reward': -1.0,
            'good': false,
          },
        ],
        'quality': {
          'exact_duplicate_groups': 0,
          'exact_duplicate_prunable': 0,
          'no_embedding': 0,
          'vague_without_anchor': 0,
          'path_or_secret_like': 0,
          'missing_source_interaction': 0,
          'missing_fts': 0,
          'orphan_fts': 0,
          'embedding_percent': 100.0,
        },
      },
    });

    final health = info.learningHealth!;
    expect(health.status, 'healthy');
    expect(health.outcomeCoveragePercent, 84.0);
    expect(health.positivePercent, 96.9);
    expect(health.groundedLessons, 461);
    expect(health.distillationYield, 0.128);
    expect(health.lessonSources['seed'], 513);
    expect(health.signals.first.signal, 'tests_passed');
    expect(health.signals.last.good, isFalse);
    expect(health.quality.embeddingPercent, 100.0);
    expect(health.quality.issueCount, 0);
    expect(health.hasWarning, isFalse);
  });

  test('chatDetailed surfaces sonder_reasoning when the server sends it',
      () async {
    final client = MockClient((request) async {
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'the answer'}
              }
            ],
            'sonder_reasoning': 'step one, step two',
          }),
          200);
    });

    final reply = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test').chatDetailed(
        const [ChatMessage(role: Role.user, content: 'hi')],
      ),
      () => client,
    );

    expect(reply.text, 'the answer');
    expect(reply.reasoning, 'step one, step two');
    expect(reply.hasReasoning, isTrue);
  });

  test('chatDetailed reports no reasoning when the server omits it', () async {
    final client = MockClient((request) async {
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'the answer'}
              }
            ],
          }),
          200);
    });

    final reply = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test').chatDetailed(
        const [ChatMessage(role: Role.user, content: 'hi')],
      ),
      () => client,
    );

    expect(reply.text, 'the answer');
    expect(reply.reasoning, '');
    expect(reply.hasReasoning, isFalse);
  });

  test('chat still returns the answer text only', () async {
    final client = MockClient((request) async {
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'the answer'}
              }
            ],
            'sonder_reasoning': 'private deliberation',
          }),
          200);
    });

    final output = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test').chat(
        const [ChatMessage(role: Role.user, content: 'hi')],
      ),
      () => client,
    );

    expect(output, 'the answer');
  });

  test('command catalog parses commands, categories and popular', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(
        jsonEncode({
          'commands': [
            {
              'name': '/task_plan',
              'aliases': ['/plan'],
              'tool': 'task_plan',
              'category': 'planning',
              'risk': 'safe',
              'summary': 'Plan a task',
              'native': false,
              'usage': '/task_plan <title> <steps> [project]',
              'params': [
                {
                  'name': 'title',
                  'type': 'str',
                  'required': true,
                  'default': null
                },
                {
                  'name': 'project',
                  'type': 'str',
                  'required': false,
                  'default': 'default'
                },
              ],
            },
            {
              'name': '/file_write',
              'aliases': [],
              'tool': 'file_write',
              'category': 'files',
              'risk': 'mutation',
              'summary': 'Write a file',
              'native': false,
              'params': [
                {
                  'name': 'path',
                  'type': 'str',
                  'required': true,
                  'default': null
                },
              ],
            },
            {
              'name': '/help',
              'category': 'meta',
              'risk': 'safe',
              'summary': 'List commands',
              'native': true,
              'usage': '/help',
            },
          ],
          'categories': {
            'meta': 'Help and discovery',
            'planning': 'Plans and tasks',
            'files': 'Workspace files',
          },
          // "/nope" is not in the catalog: a popular entry the server no
          // longer publishes must be skipped, not rendered as a dead row.
          'popular': ['/help', '/nope', '/task_plan'],
        }),
        200,
      );
    });

    final catalog = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test/').fetchCommands(),
      () => client,
    );

    expect(seen.url.toString(), 'http://sonder.test/v1/commands');
    expect(catalog.commands.length, 3);
    expect(catalog.isEmpty, isFalse);

    final plan = catalog.commands.first;
    expect(plan.name, '/task_plan');
    expect(plan.aliases, ['/plan']);
    expect(plan.tool, 'task_plan');
    expect(plan.category, 'planning');
    expect(plan.risk, 'safe');
    expect(plan.native, isFalse);
    expect(plan.params.length, 2);
    expect(plan.params.first.label, 'title: str');
    expect(plan.params.last.label, '[project: str = default]');
    expect(plan.usageLine, '/task_plan <title> <steps> [project]');
    expect(plan.matchesPrefix('/pl'), isTrue, reason: 'aliases match');
    expect(plan.matchesLoose('plan a task'), isTrue);

    // No usage on the wire: synthesise one from the declared params so a
    // palette row still says what arguments the command takes.
    final write = catalog.commands[1];
    expect(write.usage, '');
    expect(write.usageLine, '/file_write path: str');
    expect(write.risk, 'mutation');

    expect(catalog.categories['meta'], 'Help and discovery');
    expect(
      catalog.popularCommands.map((c) => c.name).toList(),
      ['/help', '/task_plan'],
    );
    // Grouping follows the server's own category ordering, not insertion
    // order of the commands.
    expect(catalog.byCategory.keys.toList(), ['meta', 'planning', 'files']);
    expect(catalog.byCategory['files']!.single.name, '/file_write');
  });

  test('command catalog tolerates a partial server record', () async {
    final client = MockClient((request) async {
      return http.Response(
        jsonEncode({
          'commands': [
            {'name': '/bare'},
            {'summary': 'nameless entries are dropped'},
          ],
        }),
        200,
      );
    });

    final catalog = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test').fetchCommands(),
      () => client,
    );

    expect(catalog.commands.length, 1);
    final bare = catalog.commands.single;
    expect(bare.aliases, isEmpty);
    expect(bare.params, isEmpty);
    expect(bare.risk, '');
    expect(bare.usageLine, '/bare');
    expect(catalog.categories, isEmpty);
    // No popular list published: fall back to the head of the catalog so a
    // bare "/" is never answered with a blank panel.
    expect(catalog.popularCommands.single.name, '/bare');
    expect(catalog.byCategory.keys.single, 'other');
  });

  test('command catalog surfaces server failures', () async {
    final failing = MockClient((request) async => http.Response('nope', 503));
    await expectLater(
      http.runWithClient(
        () => const SonderApi(baseUrl: 'http://sonder.test').fetchCommands(),
        () => failing,
      ),
      throwsA(isA<SonderException>()),
    );

    final unauthorized = MockClient((request) async => http.Response('', 401));
    await expectLater(
      http.runWithClient(
        () => const SonderApi(baseUrl: 'http://sonder.test').fetchCommands(),
        () => unauthorized,
      ),
      throwsA(isA<SonderException>()),
    );
  });

  test('command completion sends the query and limit', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(
        jsonEncode({
          'matches': [
            {
              'name': '/task_plan',
              'category': 'planning',
              'risk': 'safe',
              'summary': 'Plan a task',
            },
          ],
        }),
        200,
      );
    });

    final matches = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test')
          .completeCommands('/ta', limit: 5),
      () => client,
    );

    expect(seen.url.path, '/v1/commands/complete');
    expect(seen.url.queryParameters, {'q': '/ta', 'limit': '5'});
    expect(matches.single.name, '/task_plan');
    expect(matches.single.summary, 'Plan a task');
  });

  test('command help returns the rendered topic text', () async {
    late http.Request seen;
    final client = MockClient((request) async {
      seen = request;
      return http.Response(jsonEncode({'text': 'usage: /task_plan <title> <steps>'}), 200);
    });

    final text = await http.runWithClient(
      () => const SonderApi(baseUrl: 'http://sonder.test')
          .commandHelp('/task_plan'),
      () => client,
    );

    expect(seen.url.path, '/v1/commands/help');
    expect(seen.url.queryParameters, {'topic': '/task_plan'});
    expect(text, 'usage: /task_plan <title> <steps>');
  });
}
