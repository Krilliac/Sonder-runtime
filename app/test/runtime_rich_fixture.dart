// A rich Runtime status and data source: every panel has something to
// show (agents, autopilot, the feed, policy, learning, deployment, approvals
// and the permission mode). Large-text tests and the local screenshot
// gallery use it; the values are made up, not captured from a server.
import 'dart:convert';

import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';

import 'runtime_fixtures.dart';

double _ts(Duration ago) =>
    runtimeNow.subtract(ago).millisecondsSinceEpoch / 1000;

Map<String, dynamic> richStatusJson() => {
      'status': 'ready · v2026.09.25 · up 3h 12m\nollama: 4 models · pool 2/2',
      'stats':
          'turns: 1,284  tool calls: 6,102\ncache hit: 71%  avg turn: 9.4s',
      'learn_tiers': 'tier 1 working memory: 32 turns\n'
          'tier 2 session summaries: 118\n'
          'tier 3 lessons: 974 (461 grounded)',
      'improvements': 'No urgent improvement items detected.',
      'db_path': r'C:\Users\me\.sonder\sonder.db',
      'state_home': r'C:\Users\me\.sonder',
      'models': [
        {'id': 'sonder', 'owned_by': 'local'},
        {'id': 'code', 'owned_by': 'local'},
        {'id': 'general', 'owned_by': 'local'},
        {'id': 'qwen2.5:3b', 'owned_by': 'ollama'},
      ],
      'context': {
        'session': 's-41c2',
        'project': 'engine',
        'title': 'Shader cache refactor',
        'status': 'healthy',
        'context_limit': 8192,
        'native_context_limit': 32768,
        'context_mode': 'virtual',
        'estimated_tokens': 2100,
        'context_percent': 25.6,
        'live_turns': 6,
        'max_live_turns': 16,
        'total_turns': 42,
        'turn_percent': 37.5,
        'lessons': 974,
        'facts': 8,
        'interactions': 4416,
        'memory_percent': 61.0,
        'updated_ts': '2026-09-25 12:41:12',
      },
      'agents': {
        'active_agents': 2,
        'total_agents': 5,
        'cancel_pending': 0,
        'interrupted_agents': 1,
        'tokens_in': 41200,
        'tokens_out': 8150,
        'capacity': {
          'logical_cpus': 24,
          'agent_ceiling': 16,
          'worker_slots': 8,
          'automatic_worker_slots': 8,
          'total_memory_bytes': 34359738368,
          'available_memory_bytes': 14173392076,
        },
        'agents': [
          {
            'id': 'master-7a3f',
            'role': 'master',
            'status': 'running',
            'activity': 'Reviewing the shader cache patch',
            'task': 'Make PSO compile on load, not on first draw',
            'tool_calls': 12,
            'tokens_in': 18200,
            'tokens_out': 3100,
          },
          {
            'id': 'sub-91ce',
            'role': 'coder',
            'status': 'running',
            'activity': 'Editing src/render/pso_cache.cpp',
            'task': 'Warm the PSO cache in PrecompilePipelines()',
            'tool_calls': 7,
            'tokens_in': 9400,
            'tokens_out': 2050,
          },
          {
            'id': 'master-2b18',
            'role': 'master',
            'status': 'interrupted',
            'activity': 'Stopped when the server restarted',
            'task': 'Port the asset importer tests',
            'tool_calls': 31,
            'tokens_in': 13600,
            'tokens_out': 3000,
          },
        ],
        'events': [
          {'agent_id': 'sub-91ce', 'message': 'edit src/render/pso_cache.cpp'},
          {'agent_id': 'master-7a3f', 'message': 'delegated: warm PSO cache'},
          {'agent_id': 'master-2b18', 'message': 'interrupted: server restart'},
        ],
      },
      'autopilot': {
        'active_runs': 1,
        'resumable_runs': 0,
        'total_runs': 3,
        'latest': {
          'id': 'auto-885ca53e8ef6',
          'objective':
              'Make PSO compilation happen at load time and prove the stall is gone.',
          'project': 'engine',
          'tier': 'code',
          'policy': 'workspace',
          'allow_web': false,
          'status': 'running',
          'phase': 'executing',
          'cycles': 2,
          'failures': 0,
          'checkpoints': 1,
          'replans': 0,
          'max_failures': 2,
          'max_tasks': 6,
          'max_replans': 2,
          'adaptive': true,
          'summary': '',
          'final_report': '',
          'last_error': '',
          'criteria': [
            'No PSO compiles on the first frame after hot-reload.',
            'The renderer test suite passes.',
          ],
          'plan': [
            {
              'id': 'task-01',
              'title': 'Find where PSOs compile',
              'instruction': 'Trace CreateGraphicsPipelineState call sites.',
              'kind': 'inspect',
              'status': 'passed',
            },
            {
              'id': 'task-02',
              'title': 'Warm the cache at load',
              'instruction': 'Add PrecompilePipelines() to the loader.',
              'kind': 'edit',
              'status': 'in_progress',
            },
            {
              'id': 'task-03',
              'title': 'Run the renderer tests',
              'instruction': 'ctest -R renderer',
              'kind': 'validate',
              'status': 'pending',
            },
          ],
        },
        'runs': const [],
        'events': [
          {'event_id': 1, 'kind': 'created', 'message': 'goal created'},
          {
            'event_id': 2,
            'kind': 'planned',
            'message': 'plan accepted (3 tasks)'
          },
        ],
      },
      'activity': {
        'active_count': 1,
        'total_tool_calls': 7,
        'latest': {
          'id': 'r1',
          'label': 'agent:code',
          'status': 'complete',
          'elapsed_ms': 2480,
          'tool_calls': 2,
          'model_calls': 3,
          'result_summary': 'Warmed the PSO cache at load and verified it.',
          'events': [
            {
              'kind': 'tool_call',
              'tool': 'file_edit',
              'title': 'Edited pso_cache.cpp',
              'command': 'edit src/render/pso_cache.cpp',
              'output': '+12 -3 lines',
              'ok': true,
              'elapsed_ms': 420,
            },
            {
              'kind': 'tool_call',
              'tool': 'run_tests',
              'title': 'Ran renderer tests',
              'command': 'ctest -R renderer',
              'output': '100% tests passed, 0 tests failed out of 38',
              'ok': true,
              'elapsed_ms': 1900,
            },
          ],
          'checklist': {
            'title': 'Warm PSO cache',
            'status': 'done',
            'items': [
              {'id': 'a', 'title': 'Find PSO creation', 'status': 'done'},
              {'id': 'b', 'title': 'Precompile at load', 'status': 'done'},
              {'id': 'c', 'title': 'Run renderer tests', 'status': 'done'},
            ],
          },
        },
        'active': const [],
      },
      'execution': {
        'known': true,
        'feed': {
          'known': true,
          'schema_version': 1,
          'active_responses': 1,
          'oldest_seq': 1,
          'next_seq': 6,
          'limits': {'events': 20},
          'events': [
            {
              'response_id': 'r1',
              'response_status': 'completed',
              'seq': 1,
              'ts': _ts(const Duration(minutes: 11)),
              'kind': 'tool_call',
              'phase': 'error',
              'tool': 'ollama_pool',
              'title': 'ollama pool: worker 2 timed out, retried on 1',
              'ok': false,
              'elapsed_ms': 30000,
            },
            {
              'response_id': 'r2',
              'response_status': 'refused',
              'seq': 2,
              'ts': _ts(const Duration(minutes: 2)),
              'kind': 'tool_call',
              'phase': 'refused',
              'tool': 'write_file',
              'title': '/write src/render/pso_cache.cpp (manual)',
            },
            {
              'response_id': 'r3',
              'response_status': 'completed',
              'seq': 3,
              'ts': _ts(const Duration(seconds: 50)),
              'kind': 'file_change',
              'action': 'edit',
              'path': 'src/render/pso_cache.cpp',
              'lines_added': 12,
              'lines_deleted': 3,
              'ok': true,
            },
            {
              'response_id': 'r3',
              'response_status': 'completed',
              'seq': 4,
              'ts': _ts(const Duration(seconds: 30)),
              'kind': 'model_call',
              'phase': 'completed',
              'model': 'sonder:latest',
              'elapsed_ms': 61200,
              'tokens_in': 2600,
              'tokens_out': 143,
              'ok': true,
              'response_preview': {
                'state': 'available',
                'text': 'The stall comes from the driver compiling the PSO '
                    'lazily on first bind.',
                'chars': 73,
                'truncated': false,
                'redacted': false,
              },
            },
          ],
        },
      },
      'runtime_policy': {
        'revision': 4,
        'path': r'C:\Users\me\AppData\Local\sonder\runtime_policy.json',
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
        'missing_models': const [],
      },
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
      'learning_health': {
        'status': 'attention',
        'interactions': 7865,
        'outcomes': 6831,
        'outcome_interactions': 6826,
        'good_outcomes': 6562,
        'bad_outcomes': 269,
        'outcome_coverage_percent': 86.8,
        'positive_percent': 96.1,
        'reviewed_positive_percent': 52.7,
        'reviewed_outcomes': 186,
        'autograded_positive_percent': 97.3,
        'autograded_outcomes': 6645,
        'lessons': 1061,
        'facts': 8,
        'grounded_lessons': 533,
        'synthetic_lessons': 528,
        'distillation_yield': 0.128,
        'lesson_sources': {'interaction': 533, 'seed': 528},
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
          'exact_duplicate_groups': 1,
          'exact_duplicate_prunable': 2,
          'no_embedding': 1,
          'vague_without_anchor': 0,
          'path_or_secret_like': 0,
          'missing_source_interaction': 0,
          'missing_fts': 0,
          'orphan_fts': 0,
          'embedding_percent': 99.6,
        },
      },
      'selfmod': {
        'enabled': true,
        'mode': 'propose',
        'active': 0,
        'deployed': 2,
        'rollback_points': 1,
        'backup_root': r'C:\Users\me\.sonder\selfmod\backups',
        'runs': [
          {
            'id': 'sm-0042',
            'phase': 'completed',
            'risk': 'low',
            'objective': 'Tighten the lesson pruner threshold',
          },
        ],
      },
      'deployment': {
        'profile': 'pooled-pair',
        'profile_id': 'two-pc',
        'local_node': 'secondary',
        'configured_members': ['secondary', 'primary'],
        'preferred_primary': 'primary',
        'control_state_scope': 'local-instance',
        'preference_confers_authority': false,
        'partition_policy':
            'no_promotion_without_fencing_and_acknowledged_data',
        'capabilities': {
          'private_compute': {
            'available': true,
            'reason': 'Configured private-node compute is enabled.',
          },
          'automatic_takeover': {
            'available': false,
            'reason':
                'Fencing and acknowledged replication are not integrated.',
          },
          'automatic_failback': {
            'available': false,
            'reason':
                'Fencing and acknowledged replication are not integrated.',
          },
          'acknowledged_state_replication': {
            'available': false,
            'reason': 'No replication backend is integrated.',
          },
          'worker_epoch_fencing': {
            'available': false,
            'reason': 'Ownership epochs are not integrated.',
          },
          'quorum': {
            'available': false,
            'reason': 'No quorum provider is integrated.',
          },
        },
        'recovery_posture': {
          'automatic_takeover_available': false,
          'automatic_failback_available': false,
          'independent_witness_required': true,
        },
      },
      'operational_capabilities': {
        'schema_version': 1,
        'control': {
          'managed_app_work': {
            'available': true,
            'reason': 'Owned dispatcher is installed.',
          },
        },
        'inference': {
          'request_level_pooling': {
            'available': true,
            'reason': 'Requests may route to one healthy worker.',
          },
          'model_sharding': {
            'available': false,
            'reason': 'Tensor sharding is not integrated.',
          },
          'pool': {
            'worker_count': 2,
            'healthy_worker_count': 2,
            'remote_worker_count': 1,
          },
        },
        'compute': {
          'local_node': 'secondary',
          'configured_peer_count': 1,
          'remote_enabled': true,
          'whole_job_placement': {
            'available': true,
            'reason': 'Complete jobs are placed on one node.',
          },
          'indefinite_scale': {
            'available': false,
            'reason': 'External provider required.',
          },
        },
        'mobility': {
          'automatic_takeover_available': false,
          'automatic_failback_available': false,
          'memory_replication_transport': {
            'available': true,
            'reason': 'Explicit, bounded, authenticated fact-only batches; '
                'an operator runs replicate_once.',
          },
          'artifact_transfer_transport': {
            'available': false,
            'reason': 'Explicit grant is disabled.',
          },
          'automatic_memory_migration': {
            'available': false,
            'reason': 'Ownership is not integrated.',
          },
          'automatic_artifact_migration': {
            'available': false,
            'reason': 'Explicit transfer only.',
          },
        },
      },
    };

FakeRuntimeData richRuntimeData() => FakeRuntimeData(
      runs: [
        runningWorkRun(),
        WorkRun(
          id: 'wr-aaaa0000000000000000000000000002',
          status: 'returned',
          createdAt: runtimeNow.subtract(const Duration(minutes: 40)),
          updatedAt: runtimeNow.subtract(const Duration(minutes: 12)),
        ),
        WorkRun(
          id: 'wr-bbbb0000000000000000000000000003',
          status: 'refused',
          createdAt: runtimeNow.subtract(const Duration(hours: 2)),
          updatedAt: runtimeNow.subtract(const Duration(hours: 1, minutes: 50)),
        ),
      ],
      approvalsPage: ApprovalsPage(supported: true, pending: [
        PendingApproval(
            callId: '3f9a12c0d1e2f3a4',
            tool: 'write_file',
            preview: 'path: src/render/pso_cache.cpp · content: (1,204 chars)',
            mode: 'manual',
            refusedAt: runtimeNow.subtract(const Duration(minutes: 2))),
        PendingApproval(
            callId: '7b2c99e0aa11bb22',
            tool: 'run_code',
            preview: 'ctest -R renderer --output-on-failure',
            mode: 'manual',
            refusedAt: runtimeNow.subtract(const Duration(minutes: 1))),
      ], open: [
        IssuedApproval(
            nonce: 'n_c41a',
            callId: '11aa22bb33cc44dd',
            tool: 'git_commit',
            expiresAt: runtimeNow.add(const Duration(minutes: 14)),
            ttlSeconds: 900),
      ]),
      ecosystemReading: EcosystemReading.parse(
          jsonDecode(jsonEncode(ecosystemReadySynthetic()))),
    )
      ..mode = PermissionMode.fromJson({
        'mode': 'manual',
        'label': 'Manual',
        'blurb': 'asks before changes',
        'elevated': false,
        'modes': [
          {'name': 'plan', 'label': 'Plan', 'blurb': 'reads only'},
          {'name': 'manual', 'label': 'Manual', 'blurb': 'asks first'},
        ],
        'matrix': {
          'read': 'allow',
          'file_write': 'ask',
          'run_program': 'ask',
          'destructive': 'deny',
        },
      })
      ..catalog = const ModelCatalog(ids: [
        'sonder',
        'code'
      ], origins: {
        'code': ModelOrigin(
            kind: 'route',
            provider: 'sonder_inference',
            servedModel: 'mock:tiny'),
      });

SystemInfo richSystemInfo() => SystemInfo.fromJson(
    jsonDecode(jsonEncode(richStatusJson())) as Map<String, dynamic>);
