/// The API surface the chat, settings and runtime screens depend on, so a
/// widget test can pass a fake instead of a [SonderApi] (plan §4).
///
/// `SonderApi` implements this. A fake implements only what its test needs
/// and throws `UnimplementedError` (or uses `noSuchMethod`) for the rest.
library;

import '../api.dart' show SystemInfo, CommandCatalog, PermissionMode;
import '../models.dart';
import 'approvals.dart';
import 'chat.dart';
import 'sessions.dart';
import 'stream.dart';
import 'transport.dart';
import 'work_runs.dart';

abstract interface class SonderApiPort {
  String get baseUrl;
  SonderEndpoint get endpoint;

  Future<List<String>> listModels();
  Future<SystemInfo> systemInfo();
  Future<CommandCatalog> fetchCommands();
  Future<PermissionMode?> fetchPermissionMode();
  Future<PermissionMode> setPermissionMode(String mode,
      {String? idempotencyKey});

  Future<String> chat(
    List<ChatMessage> messages, {
    String model,
    String contextSize,
    String sessionId,
    String project,
    bool allowApproximateLocation,
    CancelToken? cancel,
  });

  Future<ChatReply> chatDetailed(
    List<ChatMessage> messages, {
    String model,
    String contextSize,
    String sessionId,
    String project,
    bool allowApproximateLocation,
    CancelToken? cancel,
  });

  Stream<ChatStreamEvent> chatStream(
    List<ChatMessage> messages, {
    String model,
    String contextSize,
    String sessionId,
    String project,
    bool allowApproximateLocation,
    CancelToken? cancel,
  });

  Future<void> recordFeedback(
    String command, {
    String model,
    String contextSize,
    String sessionId,
    String project,
  });

  void cancelChat();

  Future<String> register(String username, String password,
      {String bootstrapSecret});
  Future<String> login(String username, String password);
  Future<void> logout();

  WorkRunsApi get workRuns;
  ApprovalsApi get approvals;
  SessionsApi get sessions;
}
