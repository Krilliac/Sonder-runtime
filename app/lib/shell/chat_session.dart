import 'dart:async';

import 'package:flutter/widgets.dart' show AppLifecycleState;

import '../api.dart';
import '../app_control.dart';
import '../chat/backend.dart';
import '../chat/controller.dart';
import '../settings.dart';

/// Builds the server seam for a settings snapshot. Tests pass a double.
typedef ChatBackendFactory = ChatBackend Function(Settings settings);

/// What the shell asks of the mounted chat page: things only the page can
/// do because they involve its composer.
abstract interface class ChatPageHandle {
  /// Open the slash-command browser; a pick goes into the composer.
  void openCommandBrowser();

  /// Put the keyboard focus in the composer.
  void focusComposer();

  /// Open the permission-mode picker; a raise still goes through the raise
  /// sheet (UX-CONTRACT), so Runtime's "Change mode" reuses this flow.
  void openPermissionModePicker();
}

/// The chat side of the app that outlives any one page: the server client,
/// the chat controller (threads, turns, polls, connection) and the
/// app-control client.
///
/// The app shell owns one, so the sidebar and the chat page share the same
/// threads, and a streaming turn keeps running while another destination
/// is shown. A [ChatScreen] pumped alone (tests) makes and owns its own.
class ChatSession {
  ChatSession({
    required Settings settings,
    ChatBackendFactory? backendFactory,
  })  : _settings = settings,
        _backendFactory = backendFactory {
    _controlScope = _contextFor(settings);
    appControl = AppControlClient(context: () => _contextFor(_settings));
    _identity = _identityOf(settings);
    api = _apiFor(settings);
    chat = ChatController(_backendFor(settings), model: settings.model)
      ..contextSize = settings.contextSize
      ..allowApproximateLocation = settings.allowApproximateLocation;
  }

  Settings _settings;
  final ChatBackendFactory? _backendFactory;
  late AppControlContext _controlScope;
  late String _identity;
  bool _started = false;
  bool _disposed = false;

  /// The settings this session last synchronized to.
  Settings get settings => _settings;

  /// The one [SonderApi] for the current server identity. Chat turns, Stop,
  /// feedback, work runs, approvals and the Agents page share it; it is
  /// replaced only when the server, key or account changes. (Building a
  /// fresh instance per access made Stop cancel nothing.)
  late SonderApi api;

  late final ChatController chat;
  late final AppControlClient appControl;

  /// The chat page while it is mounted, for the shell's shortcuts.
  ChatPageHandle? page;

  /// Bumped whenever [api] is replaced (a different server, key or
  /// account), so pollers keyed to the old identity can reset.
  int get identityGeneration => _identityGeneration;
  int _identityGeneration = 0;

  static AppControlContext _contextFor(Settings settings) => AppControlContext(
        serverUrl: settings.serverUrl,
        deploymentKey: settings.apiKey,
        account: settings.accountSession,
      );

  /// Server, key and account: a change means a different principal, so the
  /// read-only mode chip and cached state reset.
  static String _identityOf(Settings s) =>
      '${s.serverUrl}\u0000${s.apiKey}\u0000${s.accountSession?.token ?? ''}';

  static SonderApi _apiFor(Settings s) => SonderApi(
        baseUrl: s.serverUrl,
        apiKey: s.apiKey,
        accountSession: s.accountSession,
      );

  ChatBackend _backendFor(Settings s) =>
      _backendFactory?.call(s) ?? SonderApiChatBackend.withApi(api);

  /// Load threads and start the polls. Safe to call more than once.
  void start() {
    if (_started || _disposed) return;
    _started = true;
    unawaited(chat.start());
  }

  void handleLifecycle(AppLifecycleState state) {
    if (!_disposed) chat.handleLifecycle(state);
  }

  /// Follow [next]: a new settings object, or the same one edited in place
  /// (the model pill and Settings both write to it).
  void syncSettings(Settings next) {
    if (_disposed) return;
    _settings = next;
    _controlScope = _contextFor(next);
    appControl.synchronize();
    chat
      ..contextSize = next.contextSize
      ..allowApproximateLocation = next.allowApproximateLocation;
    final identity = _identityOf(next);
    final changed = identity != _identity;
    _identity = identity;
    if (changed) {
      api = _apiFor(next);
      _identityGeneration++;
    }
    chat.updateBackend(_backendFor(next), identityChanged: changed);
    chat.syncModel(next.model);
  }

  /// Settings saved [next]: a different account, origin or key means the
  /// app-control credential no longer applies and is forgotten.
  void settingsSaved(Settings next) {
    if (_disposed) return;
    final scope = _contextFor(next);
    if (!_controlScope.same(scope)) appControl.forget();
    _controlScope = scope;
  }

  void dispose() {
    if (_disposed) return;
    _disposed = true;
    page = null;
    appControl.dispose();
    chat.dispose();
  }
}
