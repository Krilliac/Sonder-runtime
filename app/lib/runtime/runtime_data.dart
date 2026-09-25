/// Read models and a small HTTP source for the Runtime detail views: work
/// runs (P1-4), jobs, fanout and compute (P1-10) and pending approvals.
///
/// Lane A owns the transport (`lib/api/**`, including `api/work_runs.dart`
/// and `api/approvals.dart`). Until those land, the Runtime screen reads
/// these routes through [RuntimeDataSource], so tests use a fake and the
/// merge only swaps [HttpRuntimeDataSource] for an adapter over lane A's
/// clients. Errors are [SonderException]s carrying `httpStatus` and `code`.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../account_session.dart';
import '../api.dart';

int? _int(Object? value) => value is num ? value.toInt() : null;

DateTime? _epoch(Object? value) {
  if (value is num && value.isFinite && value > 0) {
    return DateTime.fromMillisecondsSinceEpoch((value * 1000).round());
  }
  if (value is String) return DateTime.tryParse(value)?.toLocal();
  return null;
}

String _text(Object? value, [int limit = 160]) {
  final text = value?.toString().trim() ?? '';
  return text.length <= limit ? text : '${text.substring(0, limit)}…';
}

/// One routed HTTP work run, as `GET /v1/work-runs` lists it.
class WorkRun {
  final String id;
  final String status;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final DateTime? deadlineAt;
  final bool cancelRequested;

  const WorkRun({
    required this.id,
    required this.status,
    this.createdAt,
    this.updatedAt,
    this.deadlineAt,
    this.cancelRequested = false,
  });

  factory WorkRun.fromJson(Map<String, dynamic> json) => WorkRun(
        id: _text(json['id'], 80),
        status: _text(json['status'], 32).toLowerCase(),
        createdAt: _epoch(json['created_at']),
        updatedAt: _epoch(json['updated_at']),
        deadlineAt: _epoch(json['deadline_at']),
        cancelRequested: json['cancel_requested'] == true,
      );

  bool get running => status == 'running';

  /// `wr-7c1e…` style short id for rows; the full id stays in semantics.
  String get shortId {
    if (id.length <= 12) return id;
    return '${id.substring(0, 7)}…';
  }

  Duration age(DateTime now) =>
      createdAt == null ? Duration.zero : now.difference(createdAt!);

  Duration? get budget => createdAt == null || deadlineAt == null
      ? null
      : deadlineAt!.difference(createdAt!);
}

/// One durable job from `GET /v1/jobs` (admin).
class JobSummary {
  final String id;
  final String kind;
  final String status;
  final DateTime? updatedAt;

  const JobSummary({
    required this.id,
    required this.kind,
    required this.status,
    this.updatedAt,
  });

  factory JobSummary.fromJson(Map<String, dynamic> json) => JobSummary(
        id: _text(json['job_id'], 80),
        kind: _text(json['kind'], 48),
        status: _text(json['status'], 32).toLowerCase(),
        updatedAt: _epoch(json['updated_at']),
      );
}

/// One non-sensitive fanout history row from `GET /v1/fanout`.
class FanoutSummary {
  final String id;
  final String status;
  final int selected;
  final int answered;
  final int failed;
  final int running;
  final DateTime? updatedAt;

  const FanoutSummary({
    required this.id,
    required this.status,
    this.selected = 0,
    this.answered = 0,
    this.failed = 0,
    this.running = 0,
    this.updatedAt,
  });

  factory FanoutSummary.fromJson(Map<String, dynamic> json) => FanoutSummary(
        id: _text(json['run_id'], 80),
        status: _text(json['status'], 32).toLowerCase(),
        selected: _int(json['models_selected']) ?? 0,
        answered: _int(json['models_answered']) ?? 0,
        failed: _int(json['models_failed']) ?? 0,
        running: (_int(json['models_running']) ?? 0) +
            (_int(json['models_pending']) ?? 0),
        updatedAt: _epoch(json['updated_ts']),
      );
}

/// One compute node row from `GET /v1/compute/nodes` (admin).
class ComputeNode {
  final String id;
  final bool local;
  final String health;
  final bool stale;
  final int? activeJobs;
  final String probeError;

  const ComputeNode({
    required this.id,
    this.local = false,
    this.health = 'unknown',
    this.stale = true,
    this.activeJobs,
    this.probeError = '',
  });

  factory ComputeNode.fromJson(Map<String, dynamic> json) => ComputeNode(
        id: _text(json['node_id'], 80),
        local: json['local'] == true,
        health: _text(json['health'], 32).toLowerCase(),
        stale: json['stale'] != false,
        activeJobs: _int(json['active_jobs']),
        probeError: _text(json['probe_error']),
      );
}

/// One pending call or open approval from `GET /v1/approvals` (server S2).
class PendingApproval {
  final String callId;
  final String tool;
  final String preview;
  final String nonce;
  final bool open;

  const PendingApproval({
    required this.callId,
    required this.tool,
    this.preview = '',
    this.nonce = '',
    this.open = false,
  });

  factory PendingApproval.fromJson(Map<String, dynamic> json,
          {bool open = false}) =>
      PendingApproval(
        callId: _text(json['call_id'], 64),
        tool: _text(json['tool'], 64),
        preview: _text(json['preview'] ?? json['summary']),
        nonce: _text(json['nonce'], 64),
        open: open,
      );
}

class ApprovalsPage {
  /// False when the server predates HTTP approvals (a 404 on the route).
  final bool supported;
  final List<PendingApproval> pending;
  final List<PendingApproval> open;

  const ApprovalsPage({
    required this.supported,
    this.pending = const [],
    this.open = const [],
  });
}

/// Everything the Runtime detail views read beyond `/v1/sonder/status`.
abstract interface class RuntimeDataSource {
  Future<List<WorkRun>> workRuns();
  Future<WorkRun?> cancelWorkRun(String id);
  Future<List<JobSummary>> jobs();
  Future<List<FanoutSummary>> fanoutRuns();
  Future<List<ComputeNode>> computeNodes();
  Future<ApprovalsPage> approvals();
}

/// Direct HTTP reads bound to the configured server only (no fallback).
class HttpRuntimeDataSource implements RuntimeDataSource {
  final String baseUrl;
  final String apiKey;
  final AccountSession? accountSession;
  final Duration timeout;

  const HttpRuntimeDataSource({
    required this.baseUrl,
    this.apiKey = '',
    this.accountSession,
    this.timeout = const Duration(seconds: 20),
  });

  Uri _uri(String path, [Map<String, String>? query]) {
    final root = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$root$path').replace(queryParameters: query);
  }

  Map<String, String> get _headers {
    final headers = <String, String>{'Accept': 'application/json'};
    if (apiKey.trim().isNotEmpty) {
      headers['Authorization'] = 'Bearer ${apiKey.trim()}';
    }
    final account = accountSession;
    if (account != null && account.matches(baseUrl)) {
      headers['X-Sonder-Account-Token'] = account.token;
    }
    return headers;
  }

  Future<Object?> _send(String method, Uri uri, String fallback) async {
    final client = http.Client();
    try {
      final request = http.Request(method, uri)
        ..followRedirects = false
        ..headers.addAll(_headers);
      if (method == 'POST') {
        request.headers['Content-Type'] = 'application/json';
        request.body = '{}';
      }
      final streamed = await client.send(request).timeout(timeout);
      final response = await http.Response.fromStream(streamed);
      Object? decoded;
      try {
        decoded = jsonDecode(utf8.decode(response.bodyBytes));
      } catch (_) {
        decoded = null;
      }
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final error = decoded is Map ? decoded['error'] : null;
        final message = error is Map ? _text(error['message'], 400) : '';
        final code = error is Map ? _text(error['code'], 64) : '';
        throw SonderException(
          message.isEmpty
              ? '$fallback (HTTP ${response.statusCode}).'
              : message,
          httpStatus: response.statusCode,
          code: code,
          retryAfterSeconds:
              int.tryParse(response.headers['retry-after'] ?? ''),
        );
      }
      if (decoded == null) {
        throw SonderException('$fallback: unreadable response.');
      }
      return decoded;
    } on SonderException {
      rethrow;
    } on TimeoutException catch (error) {
      throw SonderException('$fallback: the server did not answer in time.',
          cause: error);
    } catch (error) {
      throw SonderException('$fallback: cannot reach the server.',
          cause: error);
    } finally {
      client.close();
    }
  }

  List<Map<String, dynamic>> _rows(Object? decoded, String key) {
    final rows = decoded is Map ? decoded[key] : null;
    return rows is List ? rows.whereType<Map<String, dynamic>>().toList() : [];
  }

  @override
  Future<List<WorkRun>> workRuns() async => _rows(
          await _send('GET', _uri('/v1/work-runs'), 'Could not load work runs'),
          'runs')
      .map(WorkRun.fromJson)
      .toList();

  @override
  Future<WorkRun?> cancelWorkRun(String id) async {
    if (!RegExp(r'^wr-[0-9a-f]{32}$').hasMatch(id)) {
      throw ArgumentError.value(id, 'id', 'not a work run id');
    }
    final decoded = await _send('POST', _uri('/v1/work-runs/$id/cancel'),
        'Could not stop the work run');
    return decoded is Map<String, dynamic> ? WorkRun.fromJson(decoded) : null;
  }

  @override
  Future<List<JobSummary>> jobs() async => _rows(
          await _send(
              'GET', _uri('/v1/jobs', {'limit': '20'}), 'Could not load jobs'),
          'data')
      .map(JobSummary.fromJson)
      .toList();

  @override
  Future<List<FanoutSummary>> fanoutRuns() async => _rows(
          await _send('GET', _uri('/v1/fanout', {'limit': '20'}),
              'Could not load fanout runs'),
          'runs')
      .map(FanoutSummary.fromJson)
      .toList();

  @override
  Future<List<ComputeNode>> computeNodes() async => _rows(
          await _send('GET', _uri('/v1/compute/nodes', {'limit': '32'}),
              'Could not load compute nodes'),
          'nodes')
      .map(ComputeNode.fromJson)
      .toList();

  @override
  Future<ApprovalsPage> approvals() async {
    try {
      final decoded =
          await _send('GET', _uri('/v1/approvals'), 'Could not load approvals');
      return ApprovalsPage(
        supported: true,
        pending: _rows(decoded, 'pending')
            .map((row) => PendingApproval.fromJson(row))
            .toList(),
        open: _rows(decoded, 'approvals')
            .map((row) => PendingApproval.fromJson(row, open: true))
            .toList(),
      );
    } on SonderException catch (error) {
      if (error.httpStatus == 404) {
        return const ApprovalsPage(supported: false);
      }
      rethrow;
    }
  }
}
