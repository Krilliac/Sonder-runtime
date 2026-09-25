import 'dart:async';


import '../account_session.dart';
import '../api.dart';
import '../models.dart';
import '../settings.dart';

/// Everything the chat workspace needs from a server, behind one seam.
///
/// The chat controller talks only to this interface so its behaviour
/// (streaming, cancellation, work runs, approvals, the status poll) is
/// testable with a double. [SonderApiChatBackend] adapts lane A's
/// [SonderApiPort] (`chatStream(cancel:)`, `recordFeedback`, `workRuns`,
/// `approvals`) to it.
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

  Future<WorkRun> getWorkRun(String id);
  Future<WorkRun> cancelWorkRun(String id);
  Future<List<WorkRun>> listWorkRuns();

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

/// The adapter over lane A's [SonderApiPort].
///
///  * One long-lived API instance per backend (per server identity): Stop,
///    feedback, work runs and approvals all talk to the same [SonderApi].
///  * Each turn owns its own [CancelToken], so Stop cancels exactly that
///    turn's request and never a passive feedback call or another turn.
///  * Turns stream through [SonderApiPort.chatStream] (deltas, then the
///    final reply); a server that answers with plain JSON still yields one
///    [TurnDone].
///  * Work runs and approvals use lane A's [WorkRunsApi] / [ApprovalsApi].
class SonderApiChatBackend implements ChatBackend {
  final SonderApiPort api;

  SonderApiChatBackend.withApi(this.api);

  SonderApiChatBackend({
    required String baseUrl,
    String apiKey = '',
    AccountSession? accountSession,
  }) : api = SonderApi(
          baseUrl: baseUrl,
          apiKey: apiKey,
          accountSession: accountSession,
        );

  factory SonderApiChatBackend.fromSettings(Settings settings) =>
      SonderApiChatBackend(
        baseUrl: settings.serverUrl,
        apiKey: settings.apiKey,
        accountSession: settings.accountSession,
      );

  @override
  String get serverUrl => api.baseUrl;

  @override
  ChatTurn startTurn(TurnRequest request) => _ApiTurn(api, request);

  @override
  Future<void> recordFeedback(String command, TurnRequest context) =>
      api.recordFeedback(
        command,
        model: context.model,
        contextSize: context.contextSize,
        sessionId: context.sessionId,
        project: context.project,
      );

  @override
  Future<SystemInfo> systemInfo() => api.systemInfo();

  @override
  Future<List<String>> listModels() => api.listModels();

  @override
  Future<CommandCatalog> fetchCommands() => api.fetchCommands();

  @override
  Future<PermissionMode?> fetchPermissionMode() => api.fetchPermissionMode();

  @override
  Future<PermissionMode> setPermissionMode(String mode) async {
    try {
      return await api.setPermissionMode(mode);
    } on SonderException catch (e) {
      throw normalizeModeError(e);
    }
  }

  /// Work runs need a developer or admin account; say so in those words.
  Future<T> _workRuns<T>(Future<T> Function(WorkRunsApi runs) call) async {
    try {
      return await call(api.workRuns);
    } on SonderException catch (e) {
      if (e.httpStatus == 403) {
        throw e.copyWith(
            message: 'Work runs need a developer or admin account.');
      }
      rethrow;
    }
  }

  @override
  Future<WorkRun> getWorkRun(String id) => _workRuns((r) => r.get(id));

  @override
  Future<WorkRun> cancelWorkRun(String id) => _workRuns((r) => r.cancel(id));

  @override
  Future<List<WorkRun>> listWorkRuns() => _workRuns((r) => r.list());

  @override
  Future<ApprovalOutcome> approveCall(String callId,
      {Duration ttl = const Duration(minutes: 15)}) async {
    try {
      final issued = await api.approvals.approve(callId, ttl: ttl);
      return ApprovalOutcome(ApprovalStatus.approved,
          nonce: issued.nonce,
          ttlSeconds:
              issued.ttlSeconds > 0 ? issued.ttlSeconds : ttl.inSeconds);
    } on SonderException catch (e) {
      if (e.code == ApprovalsApi.unavailableCode) {
        return const ApprovalOutcome(ApprovalStatus.unsupported);
      }
      if (e.httpStatus == 401 || e.httpStatus == 403) {
        return const ApprovalOutcome(ApprovalStatus.forbidden,
            message: 'Approvals need a developer or admin account.');
      }
      return ApprovalOutcome(ApprovalStatus.failed, message: e.message);
    }
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
  final CancelToken _cancel = CancelToken();
  final _controller = StreamController<TurnEvent>();

  _ApiTurn(SonderApiPort api, TurnRequest request) {
    unawaited(_run(api, request));
  }

  Future<void> _run(SonderApiPort api, TurnRequest r) async {
    try {
      await for (final event in api.chatStream(
        r.history,
        model: r.model,
        contextSize: r.contextSize,
        sessionId: r.sessionId,
        project: r.project,
        allowApproximateLocation: r.allowApproximateLocation,
        history: r.historyMode,
        cancel: _cancel,
      )) {
        if (_cancel.isCancelled) break;
        switch (event) {
          case ChatStreamDelta(:final text):
            if (text.isNotEmpty) _controller.add(TurnDelta(text));
          case ChatStreamDone(:final reply):
            _controller.add(TurnDone(reply));
          case ChatStreamOpened() || ChatStreamKeepAlive():
            break;
        }
      }
    } catch (e, st) {
      if (!_cancel.isCancelled) _controller.addError(e, st);
    } finally {
      if (!_controller.isClosed) await _controller.close();
    }
  }

  @override
  Stream<TurnEvent> get events => _controller.stream;

  /// Cancels this turn's request only (its own token).
  @override
  void cancel() => _cancel.cancel();
}
