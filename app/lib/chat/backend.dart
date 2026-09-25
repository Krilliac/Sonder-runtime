import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../account_session.dart';
import '../api.dart';
import '../models.dart';
import '../settings.dart';

/// Everything the chat workspace needs from a server, behind one seam.
///
/// The chat controller talks only to this interface so its behaviour
/// (streaming, cancellation, work runs, approvals, the status poll) is
/// testable with a double, and so lane A's transport rewrite (`lib/api/**`:
/// `chatDetailed(cancel:)`, `api/stream.dart`, `api/work_runs.dart`,
/// `api/approvals.dart`) plugs in by changing [SonderApiChatBackend] alone.
abstract class ChatBackend {
  /// The server this backend talks to, for display.
  String get serverUrl;

  /// Start one turn. The returned [ChatTurn] streams events until a
  /// [TurnDone] (then closes) or an error; [ChatTurn.cancel] closes it.
  ChatTurn startTurn(TurnRequest request);

  /// Passive learning feedback (`/accept`, `/edited`, `/copied`). Owns its
  /// own request and is never cancelled by Stop.
  Future<void> recordFeedback(String command, TurnRequest context);

  Future<SystemInfo> systemInfo();
  Future<List<String>> listModels();
  Future<CommandCatalog> fetchCommands();
  Future<PermissionMode?> fetchPermissionMode();
  Future<PermissionMode> setPermissionMode(String mode);

  Future<WorkRunInfo> getWorkRun(String id);
  Future<WorkRunInfo> cancelWorkRun(String id);
  Future<List<WorkRunInfo>> listWorkRuns();

  /// Approve exactly one refused call once (`POST /v1/approvals/<call_id>`).
  Future<ApprovalOutcome> approveCall(String callId, {Duration ttl});

  /// Release anything held (open turns, clients).
  void dispose() {}
}

/// What to send for one turn.
class TurnRequest {
  final List<ChatMessage> history;
  final String model;
  final String contextSize;
  final String sessionId;
  final String project;
  final bool allowApproximateLocation;

  /// `'client'` asks the server not to inject its durable history (server
  /// S5). Set after a session rotation, so a cancelled first turn cannot be
  /// resurrected from the server side.
  final String? historyMode;

  const TurnRequest({
    required this.history,
    required this.model,
    this.contextSize = '8192',
    this.sessionId = '',
    this.project = '',
    this.allowApproximateLocation = false,
    this.historyMode,
  });
}

/// Events of one turn, in order: any number of [TurnPhase] / [TurnDelta],
/// then exactly one [TurnDone]. Failures arrive as a stream error.
sealed class TurnEvent {
  const TurnEvent();
}

/// The server reported what it is doing (`routing`, `reading files`, …).
class TurnPhase extends TurnEvent {
  final String phase;
  final int? tokensIn;
  const TurnPhase(this.phase, {this.tokensIn});
}

/// More answer text. Appended in order.
class TurnDelta extends TurnEvent {
  final String text;
  const TurnDelta(this.text);
}

/// The final reply. Its text replaces whatever the deltas built up.
class TurnDone extends TurnEvent {
  final ChatReply reply;
  const TurnDone(this.reply);
}

/// One in-flight turn. [cancel] is idempotent and closes the request that
/// belongs to *this* turn only.
abstract class ChatTurn {
  Stream<TurnEvent> get events;
  void cancel();
}

/// A persisted HTTP work run (`GET /v1/work-runs/<id>`).
class WorkRunInfo {
  final String id;
  final String status;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final DateTime? deadlineAt;
  final bool cancelRequested;
  final String output;
  final bool outputTruncated;

  const WorkRunInfo({
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

  factory WorkRunInfo.fromJson(Map<String, dynamic> json) {
    DateTime? ts(Object? v) {
      if (v is num) {
        return DateTime.fromMillisecondsSinceEpoch((v * 1000).round());
      }
      final parsed = double.tryParse(v?.toString() ?? '');
      if (parsed != null) {
        return DateTime.fromMillisecondsSinceEpoch((parsed * 1000).round());
      }
      return DateTime.tryParse(v?.toString() ?? '');
    }

    String text(Object? v, int limit) {
      final s = v?.toString() ?? '';
      return s.length <= limit ? s : s.substring(0, limit);
    }

    return WorkRunInfo(
      id: text(json['id'], 64),
      status: text(json['status'], 32),
      createdAt: ts(json['created_at']),
      updatedAt: ts(json['updated_at']),
      deadlineAt: ts(json['deadline_at']),
      cancelRequested: json['cancel_requested'] == true,
      output: text(json['output'], 200000),
      outputTruncated: json['output_truncated'] == true,
    );
  }
}

enum ApprovalStatus { approved, unsupported, forbidden, failed }

class ApprovalOutcome {
  final ApprovalStatus status;
  final String nonce;
  final int ttlSeconds;
  final String message;

  const ApprovalOutcome(
    this.status, {
    this.nonce = '',
    this.ttlSeconds = 0,
    this.message = '',
  });
}

/// The adapter over today's [SonderApi].
///
/// At 5f8c7665 the API has no streaming, no work-run or approval client and
/// no per-call cancel token, so this adapter:
///  * runs each turn on its **own** [SonderApi] instance, so Stop cancels
///    that turn's client and never a passive feedback call (P0-6 at the
///    call site; lane A removes the shared slot itself);
///  * emits a single [TurnDone] (no deltas) until lane A's `api/stream.dart`
///    lands — the UI already renders deltas;
///  * speaks the work-run and approval routes directly with the same auth
///    headers as [SonderApi]; replace with lane A's `WorkRunsApi` and
///    approvals client at merge.
class SonderApiChatBackend implements ChatBackend {
  final String baseUrl;
  final String apiKey;
  final AccountSession? accountSession;

  SonderApiChatBackend({
    required this.baseUrl,
    this.apiKey = '',
    this.accountSession,
  });

  factory SonderApiChatBackend.fromSettings(Settings settings) =>
      SonderApiChatBackend(
        baseUrl: settings.serverUrl,
        apiKey: settings.apiKey,
        accountSession: settings.accountSession,
      );

  SonderApi _api() => SonderApi(
        baseUrl: baseUrl,
        apiKey: apiKey,
        accountSession: accountSession,
      );

  @override
  String get serverUrl => baseUrl;

  @override
  ChatTurn startTurn(TurnRequest request) => _ApiTurn(_api(), request);

  @override
  Future<void> recordFeedback(String command, TurnRequest context) async {
    await _api().chatDetailed(
      [ChatMessage(role: Role.user, content: command)],
      model: context.model,
      contextSize: context.contextSize,
      sessionId: context.sessionId,
      project: context.project,
      allowApproximateLocation: context.allowApproximateLocation,
    );
  }

  @override
  Future<SystemInfo> systemInfo() => _api().systemInfo();

  @override
  Future<List<String>> listModels() => _api().listModels();

  @override
  Future<CommandCatalog> fetchCommands() => _api().fetchCommands();

  @override
  Future<PermissionMode?> fetchPermissionMode() => _api().fetchPermissionMode();

  @override
  Future<PermissionMode> setPermissionMode(String mode) async {
    try {
      return await _api().setPermissionMode(mode);
    } on SonderException catch (e) {
      throw normalizeModeError(e);
    }
  }

  Map<String, String> _headers() {
    final h = <String, String>{'Content-Type': 'application/json'};
    if (apiKey.trim().isNotEmpty) {
      h['Authorization'] = 'Bearer ${apiKey.trim()}';
    }
    if (accountSession?.matches(baseUrl) == true) {
      h['X-Sonder-Account-Token'] = accountSession!.token;
    }
    return h;
  }

  Uri _uri(String path) =>
      Uri.parse('${baseUrl.trim().replaceAll(RegExp(r'/+$'), '')}$path');

  Future<http.Response> _send(String method, String path,
      {Object? body}) async {
    final client = http.Client();
    try {
      final request = http.Request(method, _uri(path))
        ..followRedirects = false
        ..headers.addAll(_headers());
      if (body != null) request.body = jsonEncode(body);
      final streamed =
          await client.send(request).timeout(const Duration(seconds: 20));
      return await http.Response.fromStream(streamed)
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      throw SonderException.transport(e, baseUrl);
    } finally {
      client.close();
    }
  }

  SonderException _httpError(http.Response resp, String fallback) {
    var message = fallback;
    var code = '';
    try {
      final decoded = jsonDecode(utf8.decode(resp.bodyBytes));
      if (decoded is Map && decoded['error'] is Map) {
        final err = decoded['error'] as Map;
        message = err['message']?.toString() ?? message;
        code = err['code']?.toString() ?? '';
      } else if (decoded is Map && decoded['message'] != null) {
        message = decoded['message'].toString();
      }
    } catch (_) {}
    if (message.length > 512) message = '${message.substring(0, 512)}…';
    return SonderException(message,
        httpStatus: resp.statusCode,
        code: code,
        retryAfterSeconds: int.tryParse(resp.headers['retry-after'] ?? ''));
  }

  Future<WorkRunInfo> _workRun(String method, String path) async {
    final resp = await _send(method, path);
    if (resp.statusCode == 403) {
      throw SonderException('Work runs need a developer or admin account.',
          httpStatus: 403, code: 'FORBIDDEN');
    }
    if (resp.statusCode != 200) {
      throw _httpError(
          resp, 'Work run request failed (HTTP ${resp.statusCode}).');
    }
    final decoded = jsonDecode(utf8.decode(resp.bodyBytes));
    if (decoded is! Map<String, dynamic>) {
      throw SonderException('Unreadable work run response.');
    }
    return WorkRunInfo.fromJson(decoded);
  }

  @override
  Future<WorkRunInfo> getWorkRun(String id) =>
      _workRun('GET', '/v1/work-runs/${Uri.encodeComponent(id)}');

  @override
  Future<WorkRunInfo> cancelWorkRun(String id) =>
      _workRun('POST', '/v1/work-runs/${Uri.encodeComponent(id)}/cancel');

  @override
  Future<List<WorkRunInfo>> listWorkRuns() async {
    final resp = await _send('GET', '/v1/work-runs');
    if (resp.statusCode == 403) {
      throw SonderException('Work runs need a developer or admin account.',
          httpStatus: 403, code: 'FORBIDDEN');
    }
    if (resp.statusCode != 200) {
      throw _httpError(resp, 'Could not list work runs.');
    }
    final decoded = jsonDecode(utf8.decode(resp.bodyBytes));
    final runs = decoded is Map ? decoded['runs'] : null;
    if (runs is! List) return const [];
    return runs
        .whereType<Map>()
        .map((r) => WorkRunInfo.fromJson(Map<String, dynamic>.from(r)))
        .toList(growable: false);
  }

  @override
  Future<ApprovalOutcome> approveCall(String callId,
      {Duration ttl = const Duration(minutes: 15)}) async {
    final http.Response resp;
    try {
      resp = await _send('POST', '/v1/approvals/${Uri.encodeComponent(callId)}',
          body: {'ttl_seconds': ttl.inSeconds});
    } on SonderException catch (e) {
      return ApprovalOutcome(ApprovalStatus.failed, message: e.message);
    }
    if (resp.statusCode == 404 || resp.statusCode == 405) {
      return const ApprovalOutcome(ApprovalStatus.unsupported);
    }
    if (resp.statusCode == 401 || resp.statusCode == 403) {
      return const ApprovalOutcome(ApprovalStatus.forbidden,
          message: 'Approvals need a developer or admin account.');
    }
    if (resp.statusCode != 200 && resp.statusCode != 201) {
      return ApprovalOutcome(ApprovalStatus.failed,
          message: _httpError(resp, 'The approval was not accepted.').message);
    }
    var nonce = '';
    var ttlSeconds = ttl.inSeconds;
    try {
      final decoded = jsonDecode(utf8.decode(resp.bodyBytes));
      if (decoded is Map) {
        final approval =
            decoded['approval'] is Map ? decoded['approval'] as Map : decoded;
        nonce = approval['nonce']?.toString() ?? '';
        ttlSeconds = int.tryParse(approval['ttl_seconds']?.toString() ?? '') ??
            ttlSeconds;
      }
    } catch (_) {}
    return ApprovalOutcome(ApprovalStatus.approved,
        nonce: nonce, ttlSeconds: ttlSeconds);
  }

  @override
  void dispose() {}
}

/// Mode errors from today's API arrive as the stringified server map
/// (`{message: ..., code: FORBIDDEN}`). Turn them into words, and keep the
/// 403 recognisable so the chip can go read-only.
SonderException normalizeModeError(SonderException e) {
  final raw = e.message;
  final forbidden = e.httpStatus == 403 ||
      e.code == 'FORBIDDEN' ||
      raw.contains('FORBIDDEN') ||
      raw.contains('forbidden');
  if (forbidden) {
    return SonderException('Only an administrator can change the mode.',
        httpStatus: 403, code: 'FORBIDDEN', cause: e);
  }
  if (raw.contains('{') || raw.contains('Map')) {
    final match = RegExp(r'message:\s*([^,}]+)').firstMatch(raw);
    final text = match?.group(1)?.trim() ?? '';
    return SonderException(
        text.isEmpty ? 'The server did not accept the mode change.' : text,
        httpStatus: e.httpStatus,
        code: e.code,
        cause: e);
  }
  return e;
}

class _ApiTurn implements ChatTurn {
  final SonderApi _api;
  final _controller = StreamController<TurnEvent>();
  bool _cancelled = false;

  _ApiTurn(this._api, TurnRequest request) {
    unawaited(_run(request));
  }

  Future<void> _run(TurnRequest r) async {
    try {
      final reply = await _api.chatDetailed(
        r.history,
        model: r.model,
        contextSize: r.contextSize,
        sessionId: r.sessionId,
        project: r.project,
        allowApproximateLocation: r.allowApproximateLocation,
      );
      if (!_cancelled) _controller.add(TurnDone(reply));
    } catch (e, st) {
      if (!_cancelled) _controller.addError(e, st);
    } finally {
      if (!_controller.isClosed) await _controller.close();
    }
  }

  @override
  Stream<TurnEvent> get events => _controller.stream;

  @override
  void cancel() {
    if (_cancelled) return;
    _cancelled = true;
    _api.cancelChat();
  }
}
