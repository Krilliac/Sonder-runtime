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
    expect(info.models, isEmpty);
  });

  test('system info parses shared local runtime policy state', () {
    final info = SystemInfo.fromJson({
      'runtime_policy': {
        'revision': 7,
        'updated_ts': 1783731000,
        'path': r'C:\Users\natew\AppData\Local\sonder\runtime_policy.json',
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
}
