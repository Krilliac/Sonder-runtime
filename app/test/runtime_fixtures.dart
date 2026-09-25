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
}
