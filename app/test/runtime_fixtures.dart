import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';

/// Fixed clock for Runtime tests and goldens: 2026-09-25 12:41:30 local.
final runtimeNow = DateTime(2026, 9, 25, 12, 41, 30);

double _epoch(DateTime time) => time.millisecondsSinceEpoch / 1000;

/// A healthy single-PC server with one agent and three feed events.
SystemInfo healthySystemInfo({bool withAgents = true}) => SystemInfo.fromJson({
      'status': 'ready · v2026.09.25 · up 3h 12m',
      'stats': '805 checks passed',
      'learn_tiers': 'local tiers: fast, code, general',
      'improvements': '',
      'db_path': '/home/me/.sonder/sonder.db',
      'state_home': '/home/me/.sonder',
      'models': [
        {'id': 'sonder:latest', 'owned_by': 'local'},
        {'id': 'code', 'owned_by': 'local'},
      ],
      'agents': {
        'active_agents': withAgents ? 1 : 0,
        'total_agents': withAgents ? 1 : 0,
        'agents': const [],
      },
      'autopilot': {
        'active_runs': 0,
        'resumable_runs': 0,
        'total_runs': 0,
        'runs': const [],
      },
      'execution': {
        'known': true,
        'feed': {
          'known': true,
          'schema_version': 1,
          'limits': {'events': 20},
          'events': [
            {
              'response_id': 'r1',
              'response_status': 'completed',
              'seq': 1,
              'ts': _epoch(runtimeNow.subtract(const Duration(minutes: 11))),
              'kind': 'tool_call',
              'phase': 'error',
              'tool': 'ollama_pool',
              'title': 'ollama pool: worker 2 timed out, retried on 1',
              'ok': false,
            },
            {
              'response_id': 'r2',
              'response_status': 'refused',
              'seq': 2,
              'ts': _epoch(runtimeNow.subtract(const Duration(minutes: 2))),
              'kind': 'tool_call',
              'phase': 'refused',
              'tool': 'write_file',
              'title': '/write src/render/pso_cache.cpp (manual)',
            },
            {
              'response_id': 'r3',
              'response_status': 'completed',
              'seq': 3,
              'ts': _epoch(runtimeNow.subtract(const Duration(seconds: 30))),
              'kind': 'model_call',
              'phase': 'completed',
              'model': 'sonder:latest',
              'elapsed_ms': 61200,
              'ok': true,
            },
          ],
        },
      },
    });

WorkRun runningWorkRun({String id = 'wr-7c1e0000000000000000000000000001'}) =>
    WorkRun(
      id: id,
      status: 'running',
      createdAt: runtimeNow.subtract(const Duration(minutes: 4, seconds: 12)),
      updatedAt: runtimeNow,
      deadlineAt: runtimeNow.add(const Duration(minutes: 25, seconds: 48)),
    );

/// In-memory [RuntimeDataSource] that records calls.
class FakeRuntimeData implements RuntimeDataSource {
  List<WorkRun> runs;
  Object? runsError;
  ApprovalsPage approvalsPage;
  Object? approvalsError;
  List<JobSummary> jobList;
  Object? jobsError;
  List<FanoutSummary> fanoutList;
  List<ComputeNode> nodes;
  Object? computeError;

  /// Host tool inventory reads: [toolInventoryFor] answers each read (by
  /// requested category); [toolInventoryError] fails every read.
  ToolInventory Function(String? category)? toolInventoryFor;
  Object? toolInventoryError;
  ToolInventory? rediscovered;
  Object? rediscoverError;
  final List<String?> toolInventoryReads = [];
  int rediscoveries = 0;

  /// When set, inventory reads wait on it (a server still discovering).
  Future<void>? toolInventoryGate;

  /// The ecosystem read. The default is what a runtime without the route
  /// answers (404): the panel's unsupported state.
  EcosystemReading ecosystemReading;
  Object? ecosystemError;
  int ecosystemReads = 0;
  final List<String> cancelled = [];

  /// When set, cancel requests wait on it (a slow server).
  Future<void>? cancelGate;
  int workRunReads = 0;
  int jobReads = 0;

  FakeRuntimeData({
    List<WorkRun>? runs,
    this.runsError,
    this.approvalsPage = const ApprovalsPage(supported: true),
    this.approvalsError,
    this.jobList = const [],
    this.jobsError,
    this.fanoutList = const [],
    this.nodes = const [],
    this.computeError,
    this.toolInventoryFor,
    this.toolInventoryError,
    this.rediscovered,
    this.rediscoverError,
    this.ecosystemReading = const EcosystemReading.unsupportedRuntime(),
    this.ecosystemError,
  }) : runs = runs ?? [];

  @override
  Future<List<WorkRun>> workRuns() async {
    workRunReads++;
    if (runsError != null) throw runsError!;
    return runs;
  }

  @override
  Future<WorkRun?> cancelWorkRun(String id) async {
    cancelled.add(id);
    await cancelGate;
    return null;
  }

  @override
  Future<ApprovalsPage> approvals() async {
    if (approvalsError != null) throw approvalsError!;
    return approvalsPage;
  }

  @override
  Future<List<JobSummary>> jobs() async {
    jobReads++;
    if (jobsError != null) throw jobsError!;
    return jobList;
  }

  @override
  Future<List<FanoutSummary>> fanoutRuns() async => fanoutList;

  @override
  Future<List<ComputeNode>> computeNodes() async {
    if (computeError != null) throw computeError!;
    return nodes;
  }

  @override
  Future<ToolInventory> toolInventory({String? category}) async {
    toolInventoryReads.add(category);
    await toolInventoryGate;
    if (toolInventoryError != null) throw toolInventoryError!;
    return toolInventoryFor?.call(category) ?? const ToolInventory();
  }

  @override
  Future<ToolInventory> refreshToolInventory() async {
    rediscoveries++;
    if (rediscoverError != null) throw rediscoverError!;
    return rediscovered ?? const ToolInventory();
  }

  @override
  Future<EcosystemReading> ecosystem() async {
    ecosystemReads++;
    if (ecosystemError != null) throw ecosystemError!;
    return ecosystemReading;
  }
}

// Ecosystem payloads built from the integration contract, section 9 (the
// route) and section 3.6 (each provider's status). They are not captured
// from a server: the route lands in another lane. The ecosystem e2e parses a
// real captured payload through SONDER_ECOSYSTEM_JSON instead.

const _digest =
    '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08';

/// The nine BackendIdentity keys of the mock backend.
Map<String, dynamic> mockIdentityJson() => {
      'backend': 'mock',
      'model': 'mock:tiny',
      'model_digest': _digest,
      'quantization': 'none',
      'backend_version': '0.1.0',
      'tokenizer_digest': _digest.replaceAll('9', 'a'),
      'template_digest': _digest.replaceAll('f', 'e'),
      'context_tokens': 4096,
      'hardware': 'cpu',
    };

/// A `provider_status()` entry for Sonder Inference.
Map<String, dynamic> inferenceStatusJson({
  String state = 'ready',
  bool? synthetic = true,
  bool identity = true,
  String? fallback,
  int fallbackCount = 0,
  String? detail,
}) =>
    {
      'provider': 'sonder_inference',
      'state': state,
      'healthy': state == 'ready',
      'checked_at': '2026-09-25T12:41:25Z',
      'detail': detail ??
          (state == 'ready'
              ? 'ready · mock backend'
              : 'connection refused: http://127.0.0.1:11437. Start '
                  'sonder-infer serve, or set SONDER_INFERENCE_BASE_URL.'),
      'capabilities': ['chat', 'fixed-endpoint'],
      'base_url': 'http://127.0.0.1:11437',
      'version': state == 'ready' ? '0.4.0' : null,
      'api_version': state == 'ready' ? 1 : null,
      'models': state == 'ready' ? ['mock:tiny'] : <String>[],
      'synthetic': state == 'ready' ? synthetic : null,
      'identity': state == 'ready' && identity ? mockIdentityJson() : null,
      'telemetry': state == 'ready'
          ? {
              'discovery_url':
                  'http://127.0.0.1:11437/.well-known/sonder-telemetry',
              'sse_url': 'http://127.0.0.1:11437/v1/telemetry/sse',
              'ndjson_url': 'http://127.0.0.1:11437/v1/telemetry/ndjson',
            }
          : null,
      'fallback': fallback,
      'fallback_count': fallbackCount,
    };

/// A `sonder.runtime.ecosystem/1` body. [inference] null leaves Sonder
/// Inference out of the status map.
Map<String, dynamic> ecosystemJson({
  String schema = 'sonder.runtime.ecosystem/1',
  String provider = 'sonder_inference',
  Map<String, dynamic>? inference,
  Map<String, String> fallbacks = const {},
  bool exportEnabled = true,
  List<String> warnings = const [],
  List<String>? connectUrls,
  List<String> corsOrigins = const ['http://127.0.0.1:4173'],
  int dropped = 0,
}) =>
    {
      'schema': schema,
      'generated_at': '2026-09-25T12:41:30Z',
      'runtime': {
        'version': '2026.09.25',
        'instance_id': 'rt-3f9a12c0',
        'node_id': 'mypc',
        'base_url': 'http://127.0.0.1:11435',
      },
      'providers': {
        'default_generation_provider': provider,
        'tier_providers': {
          for (final tier in ['fast', 'general', 'code', 'reasoning', 'vision'])
            tier: provider,
        },
        'embedding_provider': 'ollama',
        'fallbacks': fallbacks,
        'status': {
          if (inference != null) 'sonder_inference': inference,
          'ollama': {
            'provider': 'ollama',
            'state': 'unknown',
          },
        },
      },
      'observatory': {
        'export_enabled': exportEnabled,
        'runtime_stream': exportEnabled
            ? {
                'discovery_url':
                    'http://127.0.0.1:11435/.well-known/sonder-telemetry',
                'sse_url': 'http://127.0.0.1:11435/v1/observability/events',
                'ndjson_url':
                    'http://127.0.0.1:11435/v1/observability/events?format=ndjson',
              }
            : null,
        'stats': {
          'subscribers': exportEnabled ? 1 : 0,
          'emitted_events': exportEnabled ? 1204 : 0,
          'dropped_events': dropped,
          'retained_events': exportEnabled ? 512 : 0,
          'buffer_capacity': 4096,
        },
        'cors_origins': corsOrigins,
        'connect_urls': connectUrls ??
            [
              if (exportEnabled) 'http://127.0.0.1:11435',
              if (inference != null && inference['telemetry'] != null)
                'http://127.0.0.1:11437',
            ],
        'warnings': warnings,
      },
      // An unknown field: clients must ignore it.
      'future_field': {'nested': true},
    };

/// Sonder Inference ready on the mock backend (synthetic).
Map<String, dynamic> ecosystemReadySynthetic() =>
    ecosystemJson(inference: inferenceStatusJson());

/// Sonder Inference bound but down, no fallback.
Map<String, dynamic> ecosystemUnavailable() =>
    ecosystemJson(inference: inferenceStatusJson(state: 'unavailable'));

/// Every tier on Ollama: Sonder Inference not configured.
Map<String, dynamic> ecosystemAllOllama() => ecosystemJson(provider: 'ollama');

/// Sonder Inference down with SONDER_INFERENCE_FALLBACK=ollama.
Map<String, dynamic> ecosystemFallback() => ecosystemJson(
      inference: inferenceStatusJson(
          state: 'unavailable', fallback: 'ollama', fallbackCount: 2),
      fallbacks: const {'sonder_inference': 'ollama'},
    );

/// Live export off (SONDER_OBSERVATORY_EXPORT=0), Inference ready.
Map<String, dynamic> ecosystemExportDisabled() => ecosystemJson(
    inference: inferenceStatusJson(synthetic: false), exportEnabled: false);

/// A payload from a future major version.
Map<String, dynamic> ecosystemUnknownSchema() =>
    ecosystemJson(schema: 'sonder.runtime.ecosystem/2');
