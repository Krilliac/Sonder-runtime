/// Routed work runs: `GET /v1/work-runs[/<id>]`, `POST /v1/work-runs/<id>/cancel`
/// (plan P0-8, P1-4).
///
/// A chat turn routed to a workbench/fleet/autopilot lane waits at most
/// ~240 s on the server, then answers with `sonder_receipt.chat_work
/// {status: running, work_run_id}`. The run keeps going; its answer is
/// persisted and fetched here. Runs are visible only to the principal that
/// started them and need a developer or admin account (403 otherwise).
library;

import 'transport.dart';

/// Server statuses (adapters/persistence/http_work_runs.py).
const workRunStatuses = {
  'running',
  'returned',
  'unknown',
  'refused',
  'cancelled',
  'budget_exceeded',
  'interrupted',
  'failed',
};

final RegExp _runId = RegExp(r'^wr-[0-9a-f]{32}$');

/// Whether [id] has the server's work-run id shape.
bool isWorkRunId(String id) => _runId.hasMatch(id);

/// One work run as the server reports it.
class WorkRun {
  final String id;
  final String status;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final DateTime? deadlineAt;
  final bool cancelRequested;

  /// The persisted answer. Empty in list results and while running.
  final String output;
  final bool outputTruncated;

  const WorkRun({
    required this.id,
    required this.status,
    this.createdAt,
    this.updatedAt,
    this.deadlineAt,
    this.cancelRequested = false,
    this.output = '',
    this.outputTruncated = false,
  });

  bool get isRunning => status == 'running';

  /// True once the run will not change again.
  bool get isTerminal => !isRunning;

  /// True when [output] is the answer the chat turn was waiting for.
  bool get hasAnswer => status == 'returned' && output.isNotEmpty;

  /// Wall-clock budget, when both ends are known.
  Duration? get budget => createdAt == null || deadlineAt == null
      ? null
      : deadlineAt!.difference(createdAt!);

  /// Time since the run started, measured against [now].
  Duration? elapsed(DateTime now) =>
      createdAt == null ? null : now.difference(createdAt!);

  static DateTime? _ts(Object? value) {
    if (value is num && value > 0) {
      return DateTime.fromMillisecondsSinceEpoch((value * 1000).round());
    }
    final parsed = double.tryParse(value?.toString() ?? '');
    if (parsed != null && parsed > 0) {
      return DateTime.fromMillisecondsSinceEpoch((parsed * 1000).round());
    }
    return null;
  }

  factory WorkRun.fromJson(Map<String, dynamic> json) {
    final status = json['status']?.toString() ?? '';
    final output = json['output'] is String ? json['output'] as String : '';
    return WorkRun(
      id: boundedResponseMetadata(json['id'], 64),
      status: workRunStatuses.contains(status) ? status : 'unknown',
      createdAt: _ts(json['created_at']),
      updatedAt: _ts(json['updated_at']),
      deadlineAt: _ts(json['deadline_at']),
      cancelRequested: json['cancel_requested'] == true,
      // The server already bounds the stored answer; cap again so a broken
      // proxy cannot put megabytes into one transcript message.
      output: output.length <= 200000 ? output : output.substring(0, 200000),
      outputTruncated:
          json['output_truncated'] == true || output.length > 200000,
    );
  }
}

/// Client for the work-run routes.
class WorkRunsApi {
  final SonderEndpoint endpoint;
  final Duration timeout;

  const WorkRunsApi(this.endpoint,
      {this.timeout = const Duration(seconds: 20)});

  SonderException _error(SonderException error) =>
      describeServerError(error, endpoint.serverUri);

  Future<Map<String, dynamic>> _call(String method, String path,
      {CancelToken? cancel}) async {
    final uri = endpoint.uri(path);
    final headers = endpoint.headers();
    final response = await () async {
      try {
        return method == 'GET'
            ? await requestGet(uri,
                headers: headers, timeout: timeout, cancel: cancel)
            : await requestPost(uri,
                headers: headers, body: '{}', timeout: timeout, cancel: cancel);
      } catch (error) {
        throw SonderException.transport(error, endpoint.baseUrl);
      }
    }();
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw _error(
          responseException(response, httpStatusFallback(response.statusCode)));
    }
    return decodeJsonObject(response, 'the work run');
  }

  void _checkId(String id) {
    if (!isWorkRunId(id)) {
      throw SonderException('That is not a work run id.', code: 'INVALID_ID');
    }
  }

  /// `GET /v1/work-runs/<id>`: status and, once returned, the answer.
  Future<WorkRun> get(String id, {CancelToken? cancel}) async {
    _checkId(id);
    return WorkRun.fromJson(
        await _call('GET', '/v1/work-runs/$id', cancel: cancel));
  }

  /// `POST /v1/work-runs/<id>/cancel`: stop the run's effects. Model steps
  /// already admitted still finish, but can no longer change anything.
  /// Send once per confirmed Stop; the server treats a repeat as a no-op.
  Future<WorkRun> cancel(String id) async {
    _checkId(id);
    return WorkRun.fromJson(await _call('POST', '/v1/work-runs/$id/cancel'));
  }

  /// `GET /v1/work-runs`: the caller's newest runs, without answer text.
  Future<List<WorkRun>> list({CancelToken? cancel}) async {
    final body = await _call('GET', '/v1/work-runs', cancel: cancel);
    final runs = body['runs'];
    if (runs is! List) return const [];
    return runs
        .whereType<Map>()
        .map((r) => WorkRun.fromJson(Map<String, dynamic>.from(r)))
        .where((r) => r.id.isNotEmpty)
        .toList(growable: false);
  }
}

/// Refresh backoff for a visible "work still running" card: 2 s, 5 s, then
/// 15 s for every later poll.
Duration workRunPollDelay(int attempt) => switch (attempt) {
      <= 0 => const Duration(seconds: 2),
      1 => const Duration(seconds: 5),
      _ => const Duration(seconds: 15),
    };
