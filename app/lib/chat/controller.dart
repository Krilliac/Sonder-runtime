import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart' show AppLifecycleState;

import '../api.dart';
import '../chat_store.dart';
import '../local_manager.dart';
import '../models.dart';
import '../runtime/model_routing.dart';
import '../settings.dart';
import 'backend.dart';
import 'commands.dart';
import 'connection.dart';
import 'permission_rules.dart';

/// One transcript row: a stored [message] plus client-only facts that are
/// never persisted or sent (a stable id for keys, whether Retry applies,
/// how long a failure took).
class ChatEntry {
  final int id;
  final ChatMessage message;
  final bool retryable;
  final int? elapsedMs;
  final List<ChatStreamToolEvent> toolCalls;

  const ChatEntry(this.id, this.message,
      {this.retryable = false,
      this.elapsedMs,
      this.toolCalls = const []});

  ChatEntry withMessage(ChatMessage next,
          {bool? retryable,
          int? elapsedMs,
          List<ChatStreamToolEvent>? toolCalls}) =>
      ChatEntry(id, next,
          retryable: retryable ?? this.retryable,
          elapsedMs: elapsedMs ?? this.elapsedMs,
          toolCalls: toolCalls ?? this.toolCalls);
}

/// What the in-flight turn is doing, for the live line.
class LiveTurn {
  final DateTime startedAt;
  final String phase;
  final String model;
  final int? tokensIn;

  /// Why the server is holding the turn (plain text), or null. Set by a
  /// status comment, cleared by the first answer text.
  final String? waitStatus;

  /// Active/recent `sonder_tool` calls during this turn.
  final List<ChatStreamToolEvent> toolCalls;

  /// Whole seconds since the turn started. Advanced by the controller's
  /// 1 Hz tick and never behind the wall clock, so it is right after the app
  /// was suspended and testable under fake time.
  final int elapsedSeconds;

  const LiveTurn({
    required this.startedAt,
    this.phase = 'routing',
    this.model = '',
    this.tokensIn,
    this.waitStatus,
    this.toolCalls = const [],
    this.elapsedSeconds = 0,
  });

  LiveTurn copyWith({
    String? phase,
    int? tokensIn,
    int? elapsedSeconds,
    String? waitStatus,
    bool clearWaitStatus = false,
    List<ChatStreamToolEvent>? toolCalls,
  }) =>
      LiveTurn(
        startedAt: startedAt,
        phase: phase ?? this.phase,
        model: model,
        tokensIn: tokensIn ?? this.tokensIn,
        waitStatus: clearWaitStatus ? null : waitStatus ?? this.waitStatus,
        toolCalls: toolCalls ?? this.toolCalls,
        elapsedSeconds: elapsedSeconds ?? this.elapsedSeconds,
      );
}

const workCapacityCode = 'WORK_CAPACITY_EXHAUSTED';

/// The words for a 429 WORK_CAPACITY_EXHAUSTED, instead of the server's
/// route instructions.
const workCapacityText =
    'Every work slot on the PC is busy. Stop a running work run, or retry '
    'in a moment.';

bool isWorkCapacityError(SonderException e) =>
    e.code == workCapacityCode ||
    e.message.contains('routed work capacity is busy');

/// True for a stored error message produced from a capacity refusal.
bool isWorkCapacityMessage(ChatMessage m) =>
    m.error && m.diagnostic.contains(workCapacityCode);

/// Outcome of a mode change request, for the shell to report.
enum ModeChangeOutcome { changed, declined, readOnly, failed, unchanged }

/// Chat state and behaviour, separate from widgets.
///
/// Owns messages, threads, send/cancel, the guarded status poll (P1-6),
/// session rotation (P1-9), connection state (P0-3) and the permission mode
/// (P0-4). The status poll updates [status] and [connection] only — two
/// [ValueNotifier]s that the status strip, the rail and the empty state
/// listen to — so a poll never rebuilds the transcript.
class ChatController extends ChangeNotifier {
  ChatBackend _backend;

  /// Called with a sentence for screen readers (P2-12).
  void Function(String message)? onAnnounce;

  ChatController(ChatBackend backend, {required String model})
      : _backend = backend,
        _model = model,
        connection =
            ValueNotifier(ConnectionStatus.connecting(backend.serverUrl));

  ChatBackend get backend => _backend;

  // -- Poll cadence -------------------------------------------------------
  static const idlePollInterval = Duration(seconds: 5);
  static const turnPollInterval = Duration(seconds: 1);
  static const modePollInterval = Duration(seconds: 15);

  final ValueNotifier<SystemInfo?> status = ValueNotifier<SystemInfo?>(null);
  final ValueNotifier<ConnectionStatus> connection;
  final ValueNotifier<LiveTurn?> live = ValueNotifier<LiveTurn?>(null);

  Timer? _pollTimer;
  Timer? _modeTimer;
  Timer? _liveTimer;
  bool _pollInFlight = false;
  bool _modeInFlight = false;

  /// Bumped when the server/key/account changes. A status, mode or model
  /// read that started under an older generation is discarded on return,
  /// and its in-flight flag no longer blocks the new identity's reads.
  int _backendGeneration = 0;
  bool _paused = false;
  bool _started = false;
  bool _disposed = false;

  /// Number of status requests actually issued (tests and diagnostics).
  int statusRequests = 0;

  // -- Transcript -----------------------------------------------------------
  final List<ChatEntry> _entries = <ChatEntry>[];
  List<ChatEntry> get entries => List.unmodifiable(_entries);
  List<ChatMessage> get messages => _entries.map((e) => e.message).toList();
  int _nextId = 1;

  List<ChatThread> _threads = const [];
  List<ChatThread> get threads => _threads;
  String _currentThreadId = '';
  String get currentThreadId => _currentThreadId;
  String _project = 'default';
  String get project => _project;
  bool _loadingThreads = true;
  bool get loadingThreads => _loadingThreads;

  Map<String, int> _sessions = <String, int>{};

  // -- Turn ---------------------------------------------------------------
  ChatTurn? _turn;
  StreamSubscription<TurnEvent>? _turnSub;
  Completer<void>? _turnDone;
  String _turnThreadId = '';
  int _pendingId = 0;
  int _userId = 0;
  bool get sending => _turn != null;

  /// The thread whose turn is streaming, or null when none is. It need not
  /// be the current thread: the person may have switched away mid-turn.
  String? get turnThreadId => _turn == null ? null : _turnThreadId;

  bool verboseErrors = false;

  /// The tier of the last reply (`code`, `fast`…), for the status line.
  String lastTier = '';

  // -- Model and commands -------------------------------------------------
  String _model;
  String get model => _model;
  List<String> _models = const ['sonder'];
  List<String> get models => _models;

  /// Provider bindings for picker labels; empty when the runtime cannot say.
  ModelRouting _routing = const ModelRouting();
  ModelRouting get routing => _routing;

  /// `/v1/models` row origins, the routing fallback for non-administrators.
  Map<String, ModelOrigin> _origins = const {};
  CommandCatalog catalog = fallbackCatalog;
  bool catalogFromServer = false;

  // -- Permission mode ----------------------------------------------------
  /// Null means "not known right now"; never a stale value shown as current.
  PermissionMode? _mode;
  PermissionMode? get permissionMode => _mode;

  /// The last mode that was known, kept only to show a disabled chip while
  /// offline (P2-13). Never presented as current.
  PermissionMode? _lastKnownMode;
  PermissionMode? get lastKnownMode => _lastKnownMode;
  bool _switchingMode = false;
  bool get switchingMode => _switchingMode;

  /// Set after a 403 FORBIDDEN from the mode route. The chip goes read-only
  /// until the account or server changes.
  bool _modeReadOnly = false;
  bool get modeReadOnly => _modeReadOnly;

  // -- Capabilities, role & client-side execution -------------------------
  ServerCapabilities? _capabilities;
  ServerCapabilities? get capabilities => _capabilities;

  WhoamiInfo? _whoami;
  WhoamiInfo? get whoami => _whoami;

  bool clientExecEnabled = true;
  String clientPermissionMode = 'manual';
  String clientWorkspaceRoot = '';
  String clientAgentUrl = Settings.defaultClientAgentUrl;
  String clientAgentToken = '';
  void Function(String mode)? onClientModeChanged;
  final Set<String> _handledClientCalls = <String>{};

  bool get isRestrictedUser {
    final who = _whoami;
    if (who != null) return who.isRestrictedUser;
    final caps = _capabilities;
    if (caps != null && caps.role.isNotEmpty) {
      return caps.role == 'user' ||
          (caps.publicHosting && !caps.allowedFeatures.hostAdmin);
    }
    final infoFeatures = status.value?.allowedFeatures;
    if (infoFeatures != null) return !infoFeatures.hostAdmin;
    return false;
  }

  bool get hostAdminAllowed {
    final who = _whoami;
    if (who != null) return who.allowedFeatures.hostAdmin;
    final caps = _capabilities;
    if (caps != null && caps.role.isNotEmpty) {
      return caps.allowedFeatures.hostAdmin;
    }
    final infoFeatures = status.value?.allowedFeatures;
    if (infoFeatures != null) return infoFeatures.hostAdmin;
    return true;
  }

  bool get hostBypassAllowed {
    final who = _whoami;
    if (who != null) return who.hostBypassAllowed;
    final caps = _capabilities;
    if (caps != null && caps.role.isNotEmpty) return caps.hostBypassAllowed;
    final infoBypass = status.value?.hostBypassAllowed;
    if (infoBypass != null) return infoBypass;
    return _mode?.canBypass ?? false;
  }

  bool get clientExecSupported =>
      _capabilities?.clientExecSupported ??
      _whoami?.clientExecSupported ??
      status.value?.clientExecSupported ??
      true;

  bool get isClientExecActive => clientExecEnabled && clientExecSupported;

  PermissionMode get clientModeState => PermissionMode(
        mode: clientPermissionMode,
        label: clientPermissionMode,
        blurb: modeBlurbs[clientPermissionMode] ?? '',
        elevated: false,
        canBypass: true,
        modes: [
          for (final name in permissionModes)
            PermissionModeOption(
              name: name,
              label: name,
              blurb: modeBlurbs[name] ?? '',
            ),
        ],
      );

  // ------------------------------------------------------------------------

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  /// Load threads and start the polls.
  Future<void> start() async {
    if (_started) return;
    _started = true;
    unawaited(refreshCapabilities());
    unawaited(refreshWhoami());
    unawaited(refreshModels());
    unawaited(refreshCommands());
    unawaited(pollStatus());
    unawaited(refreshPermissionMode());
    await loadThreads();
  }

  /// Cancel and discard any in-flight streamed turn so switching servers or
  /// signing in/out never lets replies bleed or persist across identities (RC8).
  void _discardActiveTurn() {
    _handledClientCalls.clear();
    final turn = _turn;
    if (turn == null) return;
    if (_turnThreadId == _currentThreadId) {
      _entries.removeWhere((e) => e.id == _pendingId && e.message.pending);
    }
    turn.cancel();
    _endTurn();
  }

  /// Point the controller at a different server/key/account. Resets
  /// everything that belonged to the old one.
  void updateBackend(ChatBackend next, {required bool identityChanged}) {
    _backend = next;
    if (identityChanged) {
      _discardActiveTurn();
      _backendGeneration++;
      _pollInFlight = false;
      _modeInFlight = false;
      _modeReadOnly = false;
      _mode = null;
      _lastKnownMode = null;
      _capabilities = null;
      _whoami = null;
      _routing = const ModelRouting();
      _origins = const {};
      connection.value = ConnectionStatus.connecting(next.serverUrl);
      status.value = null;
      _notify();
      unawaited(pollStatus());
      unawaited(refreshCapabilities());
      unawaited(refreshWhoami());
      unawaited(refreshPermissionMode());
      unawaited(refreshModels());
      unawaited(refreshCommands());
    }
  }

  // -- Status poll (P1-6) -------------------------------------------------

  /// Pause on `paused`/`hidden`/`detached`, resume (and refresh) on
  /// `resumed`. A backgrounded phone issues no status requests.
  void handleLifecycle(AppLifecycleState state) {
    final shouldPause = state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden ||
        state == AppLifecycleState.detached;
    if (shouldPause && !_paused) {
      _paused = true;
      _pollTimer?.cancel();
      _modeTimer?.cancel();
    } else if (state == AppLifecycleState.resumed && _paused) {
      _paused = false;
      unawaited(pollStatus());
      unawaited(refreshPermissionMode());
    }
  }

  bool get paused => _paused;

  void _schedulePoll() {
    _pollTimer?.cancel();
    if (_paused || _disposed || !_started) return;
    _pollTimer = Timer(
        sending ? turnPollInterval : idlePollInterval, () => pollStatus());
  }

  void _scheduleModePoll() {
    _modeTimer?.cancel();
    if (_paused || _disposed || !_started) return;
    _modeTimer = Timer(modePollInterval, () => refreshPermissionMode());
  }

  /// One status read. Never overlaps another; only notifies when something
  /// visible changed.
  Future<void> pollStatus() async {
    if (_pollInFlight || _disposed || _paused) return;
    _pollInFlight = true;
    _pollTimer?.cancel();
    statusRequests++;
    final generation = _backendGeneration;
    final backend = _backend;
    try {
      final info = await backend.systemInfo();
      if (_disposed || generation != _backendGeneration) return;
      status.value = info;
      _setConnection(ConnectionStatus(
          ConnState.connected, ConnectionStatus.hostOf(backend.serverUrl)));
    } catch (e) {
      if (_disposed || generation != _backendGeneration) return;
      status.value = null;
      _setConnection(ConnectionStatus.fromError(e, backend.serverUrl));
    } finally {
      // A stale poll neither clears the new identity's flag nor reschedules.
      if (generation == _backendGeneration) {
        _pollInFlight = false;
        _schedulePoll();
      }
    }
  }

  void _setConnection(ConnectionStatus next) {
    if (connection.value == next) return;
    final wasOffline = connection.value.isOffline;
    connection.value = next;
    // The chip's enabled state depends on reachability.
    if (wasOffline != next.isOffline) _notify();
  }

  /// Retry after a failure: status, mode and models at once.
  Future<void> retryConnection() async {
    connection.value = ConnectionStatus.connecting(_backend.serverUrl);
    _notify();
    await Future.wait([
      pollStatus(),
      refreshCapabilities(),
      refreshWhoami(),
      refreshPermissionMode(),
      refreshModels(),
    ]);
  }

  Future<void> refreshCapabilities() async {
    final generation = _backendGeneration;
    try {
      final caps = await _backend.fetchCapabilities();
      if (_disposed || generation != _backendGeneration || caps == null) return;
      _capabilities = caps;
      _notify();
    } catch (_) {
      // Older server or offline: leave null.
    }
  }

  Future<void> refreshWhoami() async {
    final generation = _backendGeneration;
    try {
      final who = await _backend.fetchWhoami();
      if (_disposed || generation != _backendGeneration || who == null) return;
      _whoami = who;
      _notify();
    } catch (_) {
      // Unauthenticated or older server: leave null.
    }
  }

  /// Switch the user's own client-local permission mode (including `bypass`
  /// after explicit confirmation via [confirm]). Never mutates the server.
  Future<(ModeChangeOutcome, String)> requestClientModeChange(
    String target, {
    required Future<bool> Function(String from, String to) confirm,
  }) async {
    final resolved = resolvePermissionMode(target);
    if (resolved == null) {
      return (ModeChangeOutcome.failed, "Unknown mode '$target'.");
    }
    if (resolved == clientPermissionMode) {
      return (ModeChangeOutcome.unchanged, '');
    }
    final generation = _backendGeneration;
    if (isModeRaise(clientPermissionMode, resolved)) {
      final ok = await confirm(clientPermissionMode, resolved);
      if (!ok || _disposed || generation != _backendGeneration) {
        return (ModeChangeOutcome.declined, '');
      }
    }
    clientPermissionMode = resolved;
    onClientModeChanged?.call(resolved);
    _notify();
    unawaited(LocalManager.setClientAgentMode(
      mode: resolved,
      confirmBypass: resolved == PermissionMode.bypass,
      companionUrl: clientAgentUrl,
      token: clientAgentToken,
    ));
    return (ModeChangeOutcome.changed, '');
  }

  /// Cycle client-local modes without ever entering `bypass`.
  String? nextClientModeInCycle() {
    final cycle = permissionModes
        .where((m) => m != PermissionMode.bypass)
        .toList(growable: false);
    if (cycle.isEmpty) return null;
    final idx = cycle.indexOf(clientPermissionMode);
    return cycle[(idx + 1) % cycle.length];
  }

  // -- Models and commands ---------------------------------------------------

  Future<void> refreshModels() async {
    final generation = _backendGeneration;
    try {
      final backend = _backend;
      final catalog = await backend.modelCatalog();
      if (_disposed || generation != _backendGeneration) return;
      if (identical(backend, _backend)) _origins = catalog.origins;
      final models = catalog.ids;
      if (models.isEmpty) return;
      _models = models;
      _model = resolveCatalogModel(_models, _model);
      _notify();
    } catch (_) {
      // Offline / no auth: keep the fallback list.
    }
    await refreshRouting();
  }

  /// Re-read the provider bindings. The ecosystem document is preferred;
  /// when it cannot be read (older runtime, 403 for a non-administrator)
  /// the `/v1/models` row origins label the picker, and with neither the
  /// labels stay plain.
  Future<void> refreshRouting() async {
    final backend = _backend;
    EcosystemStatus? status;
    try {
      status = (await backend.ecosystemStatus()).status;
    } catch (_) {
      status = null;
    }
    final next = ModelRouting.of(status, origins: _origins);
    if (_disposed || !identical(backend, _backend)) return;
    _routing = next;
    _notify();
  }

  void selectModel(String m) {
    _model = m;
    _notify();
  }

  /// Settings changed the model underneath us.
  void syncModel(String m) {
    if (m == _model) return;
    _model = m;
    _notify();
  }

  Future<void> refreshCommands() async {
    final generation = _backendGeneration;
    try {
      final next = await _backend.fetchCommands();
      if (_disposed || generation != _backendGeneration || next.isEmpty) {
        return;
      }
      catalog = next;
      catalogFromServer = true;
      _notify();
    } catch (_) {
      // Keep the fallback catalog; every command still works when typed.
    }
  }

  // -- Permission mode (P0-4) ------------------------------------------------

  Future<void> refreshPermissionMode() async {
    if (_modeInFlight || _disposed) return;
    _modeInFlight = true;
    _modeTimer?.cancel();
    final generation = _backendGeneration;
    PermissionMode? next;
    try {
      final mode = await _backend.fetchPermissionMode();
      next = (mode != null && mode.isUsable) ? mode : null;
    } catch (_) {
      next = null;
    } finally {
      if (generation == _backendGeneration) {
        _modeInFlight = false;
        _scheduleModePoll();
      }
    }
    // A mode read from the previous server/account is never applied: a
    // stale high mode would make a real raise look like a lowering.
    if (_disposed || generation != _backendGeneration) return;
    if (next != null) _lastKnownMode = next;
    if (next?.mode == _mode?.mode &&
        next?.elevated == _mode?.elevated &&
        next?.elevationReason == _mode?.elevationReason &&
        next?.canBypass == _mode?.canBypass &&
        (next == null) == (_mode == null)) {
      return;
    }
    _mode = next;
    _notify();
  }

  /// Ask to switch to [target]. Raising asks [confirm] first (the app is the
  /// attended surface, so the sheet *is* the person confirming); lowering
  /// does not. Returns what happened and, on failure, the words to show.
  Future<(ModeChangeOutcome, String)> requestModeChange(
    String target, {
    required Future<bool> Function(String from, String to) confirm,
  }) async {
    final current = _mode;
    if (current == null || _switchingMode) {
      return (ModeChangeOutcome.unchanged, '');
    }
    if (_modeReadOnly) {
      return (ModeChangeOutcome.readOnly, modeReadOnlyText);
    }
    if (target == current.mode) return (ModeChangeOutcome.unchanged, '');
    final resolvedTarget = resolvePermissionMode(target);
    if (resolvedTarget == PermissionMode.bypass && !current.canBypass) {
      return (
        ModeChangeOutcome.readOnly,
        'Only an administrator can enable bypass mode.'
      );
    }
    final generation = _backendGeneration;
    if (isModeRaise(current.mode, target)) {
      final ok = await confirm(current.mode, target);
      // A confirmation given for the previous server/account is not one for
      // the new identity.
      if (!ok || _disposed || generation != _backendGeneration) {
        return (ModeChangeOutcome.declined, '');
      }
    }
    _switchingMode = true;
    _notify();
    try {
      final next = await _backend.setPermissionMode(
        target,
        confirmBypass: resolvedTarget == PermissionMode.bypass,
      );
      if (_disposed || generation != _backendGeneration) {
        return (ModeChangeOutcome.changed, '');
      }
      _mode = next.isUsable ? next : null;
      if (_mode != null) _lastKnownMode = _mode;
      return (ModeChangeOutcome.changed, '');
    } on SonderException catch (e) {
      if (_disposed || generation != _backendGeneration) {
        return (ModeChangeOutcome.changed, '');
      }
      final err = normalizeModeError(e);
      if (err.httpStatus == 403 || err.code == 'FORBIDDEN') {
        _modeReadOnly = true;
        return (ModeChangeOutcome.readOnly, modeReadOnlyText);
      }
      // What the server holds is now unknown: drop the chip, re-read.
      _mode = null;
      unawaited(refreshPermissionMode());
      return (
        ModeChangeOutcome.failed,
        'Could not change mode: ${err.message}'
      );
    } catch (e) {
      if (_disposed || generation != _backendGeneration) {
        return (ModeChangeOutcome.changed, '');
      }
      _mode = null;
      unawaited(refreshPermissionMode());
      return (ModeChangeOutcome.failed, 'Could not change mode.');
    } finally {
      _switchingMode = false;
      _notify();
    }
  }

  /// The mode Shift+Tab would move to, or null. Never cycles into `bypass`
  /// — entering bypass requires explicit selection and confirmation.
  String? nextModeInCycle() {
    final current = _mode;
    if (current == null || current.options.isEmpty) return null;
    final cycle = current.options
        .where((o) => o.name != PermissionMode.bypass)
        .toList(growable: false);
    if (cycle.isEmpty) return null;
    final index = cycle.indexWhere((o) => o.name == current.mode);
    return cycle[(index + 1) % cycle.length].name;
  }

  // -- Threads -------------------------------------------------------------

  ChatThread get currentThread => _threads.firstWhere(
        (t) => t.id == _currentThreadId,
        orElse: () => _threads.isNotEmpty ? _threads.first : ChatThread.fresh(),
      );

  Future<void> loadThreads() async {
    final loaded = await ChatStore.load();
    _sessions = await ChatStore.loadSessions();
    if (_disposed) return;
    final current = loaded.first;
    _threads = loaded;
    _currentThreadId = current.id;
    _project = current.project;
    _setEntries(current.messages);
    _loadingThreads = false;
    _notify();
  }

  void _setEntries(List<ChatMessage> messages) {
    _entries
      ..clear()
      ..addAll(messages.map((m) => ChatEntry(_nextId++, m)));
    // The status line's tier is the last reply's route.
    for (final m in messages.reversed) {
      final tier = m.responseMetadata?.tier ?? '';
      if (tier.isNotEmpty) {
        lastTier = tier;
        break;
      }
    }
  }

  String titleFor(List<ChatMessage> messages) {
    final userMessages = messages.where((m) => m.role == Role.user);
    if (userMessages.isEmpty) return currentThread.title;
    final text =
        userMessages.first.content.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (text.isEmpty) return 'New chat';
    if (text.length <= 42) return text;
    return '${text.substring(0, 42)}...';
  }

  /// Persist the visible thread (pending rows excluded).
  Future<void> saveCurrentThread({String? project}) =>
      _saveThread(_currentThreadId, messages, project: project);

  Future<void> _saveThread(String threadId, List<ChatMessage> messages,
      {String? project}) async {
    if (threadId.isEmpty) return;
    final kept = messages.where((m) => !m.pending).toList();
    final isCurrent = threadId == _currentThreadId;
    final nextProject = (project ?? (isCurrent ? _project : null))?.trim();
    _threads = _threads.map((thread) {
      if (thread.id != threadId) return thread;
      final userMessages = kept.where((m) => m.role == Role.user);
      var title = thread.title;
      if (userMessages.isNotEmpty) {
        final text =
            userMessages.first.content.replaceAll(RegExp(r'\s+'), ' ').trim();
        title = text.isEmpty
            ? 'New chat'
            : (text.length <= 42 ? text : '${text.substring(0, 42)}...');
      }
      return thread.copyWith(
        title: title,
        project: (nextProject == null || nextProject.isEmpty)
            ? thread.project
            : nextProject,
        messages: kept,
        updatedAt: DateTime.now(),
      );
    }).toList()
      ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    if (isCurrent && nextProject != null && nextProject.isNotEmpty) {
      _project = nextProject;
    }
    _notify();
    await ChatStore.save(_threads);
  }

  void newChat() {
    final fresh = ChatThread.fresh(project: _project);
    _threads = [fresh, ..._threads];
    _currentThreadId = fresh.id;
    _project = fresh.project;
    _entries.clear();
    _notify();
    unawaited(ChatStore.save(_threads));
  }

  void switchThread(ChatThread thread) {
    if (thread.id == _currentThreadId) return;
    _currentThreadId = thread.id;
    _project = thread.project;
    final latest =
        _threads.firstWhere((t) => t.id == thread.id, orElse: () => thread);
    _setEntries(latest.messages);
    if (sending && _turnThreadId == thread.id) _restorePendingRow();
    _notify();
  }

  /// Delete [thread]. The result is what [restoreThread] needs to undo it.
  Future<DeletedThread> deleteThread(ChatThread thread) async {
    final index = _threads.indexWhere((t) => t.id == thread.id);
    final stored = index < 0 ? thread : _threads[index];
    if (sending && _turnThreadId == thread.id) cancel();
    final remaining = _threads.where((t) => t.id != thread.id).toList();
    final placeholder =
        remaining.isEmpty ? ChatThread.fresh(project: _project) : null;
    final next = placeholder == null ? remaining : [placeholder];
    final wasCurrent = thread.id == _currentThreadId;
    final current = wasCurrent ? next.first : currentThread;
    _threads = next;
    _currentThreadId = current.id;
    _project = current.project;
    _setEntries(current.messages);
    final session = _sessions.remove(thread.id);
    _notify();
    await ChatStore.save(next);
    return DeletedThread._(stored, index < 0 ? 0 : index, wasCurrent,
        session, placeholder?.id);
  }

  /// Put back a thread [deleteThread] removed, at its old position, and
  /// select it again if it was selected. An untouched placeholder created
  /// by the delete is dropped.
  Future<void> restoreThread(DeletedThread deleted) async {
    final thread = deleted.thread;
    if (_threads.any((t) => t.id == thread.id)) return;
    final next = [
      for (final t in _threads)
        if (t.id != deleted._placeholderId || t.messages.isNotEmpty) t,
    ];
    next.insert(deleted.index.clamp(0, next.length), thread);
    _threads = next;
    if (deleted._session case final n?) _sessions[thread.id] = n;
    if (deleted.wasCurrent || !next.any((t) => t.id == _currentThreadId)) {
      _currentThreadId = thread.id;
      _project = thread.project;
      _setEntries(thread.messages);
    }
    _notify();
    await ChatStore.save(next);
  }

  // -- Session rotation (P1-9) -------------------------------------------

  /// The server session for [threadId]: the thread id, or `<id>-<n>` after a
  /// cancelled first turn rotated it.
  String sessionFor(String threadId) {
    final n = _sessions[threadId] ?? 0;
    return n == 0 ? threadId : '$threadId-$n';
  }

  bool _rotated(String threadId) => (_sessions[threadId] ?? 0) > 0;

  Future<void> _rotateSession(String threadId) async {
    _sessions[threadId] = (_sessions[threadId] ?? 0) + 1;
    try {
      await ChatStore.saveSessions(_sessions);
    } catch (_) {}
  }

  // -- Local toggles -------------------------------------------------------

  /// Client-side settings that must work while the server is unreachable.
  String? localToggle(String text) {
    final t = text.trim().toLowerCase();
    bool wantsOn(String s) =>
        s.contains(' on') ||
        s.contains('enable') ||
        s.contains('show') ||
        s.contains('turn on');
    bool wantsOff(String s) =>
        s.contains(' off') ||
        s.contains('disable') ||
        s.contains('hide') ||
        s.contains('turn off');
    final isVerboseTopic = t.startsWith('/verbose') ||
        t.startsWith('/errors') ||
        (RegExp(r'\b(raw|verbose|full|detailed)\b').hasMatch(t) &&
            RegExp(r'\b(error|errors|exception|exceptions|traceback)\b')
                .hasMatch(t));
    if (!isVerboseTopic) return null;
    if (wantsOff(t)) {
      verboseErrors = false;
      return 'Verbose errors are **off**. Error messages will show the plain '
          'explanation only.';
    }
    if (wantsOn(t) || t == '/verbose' || t == '/errors') {
      verboseErrors = true;
      return 'Verbose errors are **on**. Failures will show the original '
          'exception under the explanation.\n\nTurn it off with `/verbose off` '
          '(or just ask).';
    }
    return 'Verbose errors are currently **${verboseErrors ? "on" : "off"}**. '
        'Say `/verbose on` or `/verbose off` — plain English works too.';
  }

  // -- Send / cancel -------------------------------------------------------

  ChatEntry _add(ChatMessage m) {
    final e = ChatEntry(_nextId++, m);
    _entries.add(e);
    return e;
  }

  int _indexOf(int id) => _entries.indexWhere((e) => e.id == id);

  // Slash commands must use the runtime control route ('sonder') even when
  // the person selected a non-default tier (code, fast) or concrete model.
  // File paths (/etc/passwd), comments (// foo), and prose keep that choice.
  static bool _isSlashCommand(String text) => RegExp(
        r'^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)',
      ).hasMatch(text.trim());

  void _replace(int id, ChatEntry Function(ChatEntry) update) {
    final i = _indexOf(id);
    if (i >= 0) _entries[i] = update(_entries[i]);
  }

  /// Send [text] as a user turn. Completes when the turn finishes, fails or
  /// is cancelled. Slash intercepts are the composer's job and never reach
  /// here.
  Future<void> send(String text) {
    final trimmed = text.trim();
    if (trimmed.isEmpty || sending) return Future.value();
    // Defence in depth for P0-5: whatever path reaches here (a preset, a
    // retry, a future caller), an account line with a password is never
    // added to the transcript, stored or sent.
    if (isAccountSecretLine(trimmed)) return Future.value();

    final localReply = localToggle(trimmed);
    if (localReply != null) {
      _add(ChatMessage(role: Role.user, content: trimmed));
      _add(ChatMessage(role: Role.assistant, content: localReply));
      _notify();
      return saveCurrentThread();
    }

    _add(ChatMessage(role: Role.user, content: trimmed));
    _userId = _entries.last.id;
    final history = messages;
    _add(const ChatMessage(role: Role.assistant, content: '', pending: true));
    _pendingId = _entries.last.id;
    _turnThreadId = _currentThreadId;
    final requestModel = _isSlashCommand(trimmed) ? 'sonder' : _model;
    live.value = LiveTurn(startedAt: DateTime.now(), model: requestModel);
    _liveTimer?.cancel();
    _liveTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      final current = live.value;
      if (current == null) return;
      final wall = DateTime.now().difference(current.startedAt).inSeconds;
      final next = current.elapsedSeconds + 1;
      live.value = current.copyWith(elapsedSeconds: wall > next ? wall : next);
    });

    final request = TurnRequest(
      history: history,
      model: requestModel,
      contextSize: contextSize,
      sessionId: sessionFor(_currentThreadId),
      project: _project,
      allowApproximateLocation: allowApproximateLocation,
      historyMode: _rotated(_currentThreadId) ? 'client' : null,
      clientExec: isClientExecActive,
    );
    _handledClientCalls.clear();
    final turnGeneration = _backendGeneration;
    final done = Completer<void>();
    _turnDone = done;
    final turn = _backend.startTurn(request);
    _turn = turn;
    _notify();
    // The user's words are durable before the reply exists.
    unawaited(_saveThread(_turnThreadId, history));
    // The status poll runs at 1 Hz while a turn is live.
    _schedulePoll();

    final buffer = StringBuffer();
    _turnSub = turn.events.listen(
      (event) {
        if (_disposed ||
            !identical(_turn, turn) ||
            turnGeneration != _backendGeneration) {
          return;
        }
        switch (event) {
          case TurnPhase(:final phase, :final tokensIn):
            live.value = live.value?.copyWith(phase: phase, tokensIn: tokensIn);
          case TurnStatus(:final text):
            final current = live.value;
            if (current == null) break;
            if (text.isEmpty) {
              // The server's clearing frame: the wait is over.
              if (current.waitStatus != null) {
                live.value = current.copyWith(clearWaitStatus: true);
              }
            } else if (current.waitStatus != text) {
              // The same text again is not news and must not touch the live
              // value (a status is never progress).
              live.value = current.copyWith(waitStatus: text);
            }
          case TurnTool(:final tool):
            _handleTurnTool(turn, tool, turnGeneration);
          case TurnDelta(:final text):
            if (live.value?.waitStatus != null) {
              live.value = live.value?.copyWith(clearWaitStatus: true);
            }
            buffer.write(text);
            _updatePending(buffer.toString());
          case TurnDone(:final reply):
            _finishOk(turn, reply, expectedGeneration: turnGeneration);
        }
      },
      onError: (Object e, StackTrace _) {
        if (!_disposed &&
            identical(_turn, turn) &&
            turnGeneration == _backendGeneration) {
          _finishError(turn, e, expectedGeneration: turnGeneration);
        }
      },
      onDone: () {
        // A stream that closed without a result (a dropped connection).
        if (!_disposed &&
            identical(_turn, turn) &&
            turnGeneration == _backendGeneration) {
          _finishError(
              turn,
              SonderException(
                  'The connection closed before the reply finished.',
                  retryable: true),
              expectedGeneration: turnGeneration);
        }
      },
      cancelOnError: true,
    );
    return done.future;
  }

  void _upsertLiveTool(ChatStreamToolEvent tool, {bool? awaiting}) {
    final current = live.value;
    if (current == null) return;
    final updated = List<ChatStreamToolEvent>.of(current.toolCalls);
    final key = tool.id.isNotEmpty ? tool.id : tool.callId;
    final idx = key.isEmpty
        ? -1
        : updated.indexWhere(
            (t) => (t.id.isNotEmpty ? t.id : t.callId) == key,
          );
    if (idx >= 0) {
      updated[idx] = tool.mergedOnto(updated[idx]);
    } else {
      updated.add(tool);
    }
    final isWaiting = awaiting ??
        (tool.isAwaitingApproval || tool.isAwaitingClientApproval);
    live.value = current.copyWith(
      phase: isWaiting ? 'awaiting approval' : null,
      toolCalls: List.unmodifiable(updated),
      clearWaitStatus: isWaiting,
    );
  }

  void _handleTurnTool(
    ChatTurn turn,
    ChatStreamToolEvent tool,
    int turnGeneration,
  ) {
    if (tool.isAwaitingClientResult && tool.callId.isNotEmpty) {
      final firstSeen = _handledClientCalls.add(tool.callId);
      if (!firstSeen) return;
      final risk = effectiveClientToolRisk(tool.risk, tool.tool);
      if (clientModeAllowsWithoutPrompt(clientPermissionMode, risk)) {
        _upsertLiveTool(tool, awaiting: false);
        unawaited(_executeAndSubmitClientTool(
          tool,
          expectedGeneration: turnGeneration,
          approved: true,
        ));
      } else {
        _upsertLiveTool(
          tool.copyWith(state: 'awaiting_client_approval'),
          awaiting: true,
        );
      }
      return;
    }
    _upsertLiveTool(tool);
  }

  Future<ClientToolExecResult> _executeAndSubmitClientTool(
    ChatStreamToolEvent tool, {
    required int expectedGeneration,
    required bool approved,
  }) async {
    final backend = _backend;
    final execRes = await backend.executeClientTool(
      callId: tool.callId,
      tool: tool.tool,
      args: tool.rawArgs,
      risk: tool.risk,
      mode: clientPermissionMode,
      workspaceRoot: clientWorkspaceRoot,
      approved: approved,
      companionUrl: clientAgentUrl,
      companionToken: clientAgentToken,
    );
    if (_disposed || expectedGeneration != _backendGeneration) {
      return execRes;
    }
    if (execRes.companionUnavailable) {
      _upsertLiveTool(
        tool.copyWith(
          state: 'awaiting_client_approval',
          error: execRes.error,
        ),
        awaiting: true,
      );
      return execRes;
    }
    if (execRes.needsApproval) {
      _upsertLiveTool(
        tool.copyWith(state: 'awaiting_client_approval'),
        awaiting: true,
      );
      return execRes;
    }
    final session = tool.session.isNotEmpty
        ? tool.session
        : sessionFor(_turnThreadId.isNotEmpty ? _turnThreadId : _currentThreadId);
    await backend.submitClientToolResult(
      tool.callId,
      ok: execRes.ok,
      output: execRes.output,
      error: execRes.error,
      denied: execRes.denied,
      session: session,
    );
    return execRes;
  }

  ChatStreamToolEvent? _findClientToolEvent(String callId) {
    final liveTools = live.value?.toolCalls;
    if (liveTools != null) {
      for (final t in liveTools) {
        if (t.callId == callId) return t;
      }
    }
    for (final e in _entries) {
      for (final t in e.toolCalls) {
        if (t.callId == callId) return t;
      }
    }
    return null;
  }

  void _updateEntryTool(
      String callId, ChatStreamToolEvent Function(ChatStreamToolEvent) update) {
    var changed = false;
    for (var i = 0; i < _entries.length; i++) {
      final tools = _entries[i].toolCalls;
      final idx = tools.indexWhere((t) => t.callId == callId);
      if (idx >= 0) {
        final nextTools = List<ChatStreamToolEvent>.of(tools);
        nextTools[idx] = update(nextTools[idx]);
        _entries[i] =
            _entries[i].withMessage(_entries[i].message, toolCalls: List.unmodifiable(nextTools));
        changed = true;
      }
    }
    if (changed) _notify();
  }

  /// Execute a paused `execution_target="client"` tool on the user's PC and
  /// POST the result to `POST /v1/client-tools/<call_id>/result`.
  Future<ApprovalOutcome> approveClientTool(
    String callId,
    String toolName,
    String digest,
  ) async {
    final ev = _findClientToolEvent(callId);
    if (ev == null) {
      return const ApprovalOutcome(ApprovalStatus.failed,
          message: 'Client tool call is no longer active.');
    }
    final generation = _backendGeneration;
    final backend = _backend;
    final execRes = await backend.executeClientTool(
      callId: callId,
      tool: ev.tool.isNotEmpty ? ev.tool : toolName,
      args: ev.rawArgs,
      risk: ev.risk,
      mode: clientPermissionMode,
      workspaceRoot: clientWorkspaceRoot,
      approved: true,
      companionUrl: clientAgentUrl,
      companionToken: clientAgentToken,
    );
    if (_disposed || generation != _backendGeneration) {
      return const ApprovalOutcome(ApprovalStatus.failed,
          message: 'Server or account changed.');
    }
    if (execRes.companionUnavailable) {
      _upsertLiveTool(
        ev.copyWith(state: 'awaiting_client_approval', error: execRes.error),
        awaiting: true,
      );
      _updateEntryTool(
        callId,
        (t) => t.copyWith(state: 'awaiting_client_approval', error: execRes.error),
      );
      return ApprovalOutcome(ApprovalStatus.failed, message: execRes.error);
    }
    final session = ev.session.isNotEmpty
        ? ev.session
        : sessionFor(_turnThreadId.isNotEmpty ? _turnThreadId : _currentThreadId);
    try {
      await backend.submitClientToolResult(
        callId,
        ok: execRes.ok,
        output: execRes.output,
        error: execRes.error,
        denied: execRes.denied,
        session: session,
      );
    } on SonderException catch (e) {
      return ApprovalOutcome(ApprovalStatus.failed, message: e.message);
    } catch (e) {
      return ApprovalOutcome(ApprovalStatus.failed, message: '$e');
    }
    if (_disposed || generation != _backendGeneration) {
      return const ApprovalOutcome(ApprovalStatus.failed,
          message: 'Server or account changed.');
    }
    _updateEntryTool(
      callId,
      (t) => t.copyWith(
        state: execRes.ok ? 'done' : 'error',
        phase: execRes.ok ? 'finished' : 'error',
        ok: execRes.ok,
        error: execRes.error.isEmpty ? null : execRes.error,
        clearError: execRes.error.isEmpty,
        result: ToolRunResult(ok: execRes.ok, output: execRes.output),
      ),
    );
    return const ApprovalOutcome(ApprovalStatus.approved);
  }

  /// Deny a paused `execution_target="client"` tool on the user's PC and
  /// report denial to `POST /v1/client-tools/<call_id>/result`.
  Future<ApprovalOutcome> denyClientTool(
    String callId,
    String toolName,
    String digest,
  ) async {
    final ev = _findClientToolEvent(callId);
    final generation = _backendGeneration;
    final backend = _backend;
    final session = (ev?.session.isNotEmpty == true)
        ? ev!.session
        : sessionFor(_turnThreadId.isNotEmpty ? _turnThreadId : _currentThreadId);
    try {
      await backend.submitClientToolResult(
        callId,
        ok: false,
        denied: true,
        error: 'Denied by user on client PC.',
        session: session,
      );
    } on SonderException catch (e) {
      return ApprovalOutcome(ApprovalStatus.failed, message: e.message);
    } catch (e) {
      return ApprovalOutcome(ApprovalStatus.failed, message: '$e');
    }
    if (_disposed || generation != _backendGeneration) {
      return const ApprovalOutcome(ApprovalStatus.failed,
          message: 'Server or account changed.');
    }
    _updateEntryTool(
      callId,
      (t) => t.copyWith(
        state: 'denied',
        phase: 'denied',
        ok: false,
        denied: true,
        error: 'Denied by user on client PC.',
      ),
    );
    return const ApprovalOutcome(ApprovalStatus.approved);
  }

  /// Chat request settings the shell keeps in sync from [Settings].
  String contextSize = '8192';
  bool allowApproximateLocation = false;

  void _updatePending(String partial) {
    if (_turnThreadId != _currentThreadId) return;
    _replace(
        _pendingId, (e) => e.withMessage(e.message.copyWith(content: partial)));
    _notify();
  }

  /// Re-add the pending row when returning to the thread whose turn is live.
  ///
  /// [_setEntries] gives every row a fresh id, so the turn's user row is
  /// found again: it is the thread's last user message, since nothing else
  /// can be sent to this thread while its turn is live. Stop then restores
  /// the prompt and rotates a cancelled first turn's session.
  void _restorePendingRow() {
    for (final e in _entries.reversed) {
      if (e.message.role == Role.user) {
        _userId = e.id;
        break;
      }
    }
    if (_entries.isNotEmpty && _entries.last.message.pending) {
      _pendingId = _entries.last.id;
      return;
    }
    _add(const ChatMessage(role: Role.assistant, content: '', pending: true));
    _pendingId = _entries.last.id;
  }

  /// Milliseconds the live turn has run, by the same clock as the live line.
  int? _liveElapsedMs() {
    final current = live.value;
    if (current == null) return null;
    final wall = DateTime.now().difference(current.startedAt).inMilliseconds;
    final ticks = current.elapsedSeconds * 1000;
    return wall > ticks ? wall : ticks;
  }

  void _endTurn() {
    _liveTimer?.cancel();
    _liveTimer = null;
    _turn = null;
    _turnSub?.cancel();
    _turnSub = null;
    live.value = null;
    final done = _turnDone;
    _turnDone = null;
    if (done != null && !done.isCompleted) done.complete();
    _schedulePoll();
  }

  Future<void> _deliver(ChatMessage message,
      {bool retryable = false,
      int? elapsedMs,
      List<ChatStreamToolEvent> toolCalls = const [],
      int? expectedGeneration}) async {
    if (_disposed ||
        (expectedGeneration != null &&
            expectedGeneration != _backendGeneration)) {
      return;
    }
    final threadId = _turnThreadId;
    if (threadId == _currentThreadId) {
      _replace(
          _pendingId,
          (e) => ChatEntry(e.id, message,
              retryable: retryable,
              elapsedMs: elapsedMs,
              toolCalls: toolCalls));
      await _saveThread(threadId, messages);
    } else {
      // The user moved to another thread: the reply still belongs to the
      // thread that asked.
      final thread = _threads.where((t) => t.id == threadId).firstOrNull;
      if (thread != null) {
        await _saveThread(threadId, [...thread.messages, message]);
      }
    }
  }

  void _finishOk(ChatTurn turn, ChatReply reply, {int? expectedGeneration}) {
    final generation = expectedGeneration ?? _backendGeneration;
    if (_disposed || generation != _backendGeneration) return;
    final elapsed = _liveElapsedMs();
    final liveTools = live.value?.toolCalls ?? const <ChatStreamToolEvent>[];
    final handoffTools = <ChatStreamToolEvent>[...liveTools];
    final seenIds = {for (final t in liveTools) if (t.callId.isNotEmpty) t.callId};
    final autoHandoffs = <ChatStreamToolEvent>[];
    for (final h in reply.metadata?.clientToolHandoffs ?? const <ClientToolHandoff>[]) {
      if (h.callId.isNotEmpty && !seenIds.contains(h.callId)) {
        seenIds.add(h.callId);
        final effectiveRisk = effectiveClientToolRisk(h.risk, h.tool);
        final allowed =
            clientModeAllowsWithoutPrompt(clientPermissionMode, effectiveRisk);
        final ev = ChatStreamToolEvent.fromJson({
          'id': h.callId,
          'call_id': h.callId,
          'execution_target': h.executionTarget,
          'backend': h.executionTarget,
          'phase': h.phase,
          'state': allowed ? 'running' : 'awaiting_client_approval',
          'tool': h.tool,
          'args': h.args,
          'risk': effectiveRisk,
          'session': h.session,
        });
        handoffTools.add(ev);
        if (allowed) autoHandoffs.add(ev);
      }
    }
    if (reply.metadata?.tier.isNotEmpty == true) {
      lastTier = reply.metadata!.tier;
    }
    final message = ChatMessage(
      role: Role.assistant,
      content: reply.text.isEmpty ? '(empty response)' : reply.text,
      reasoning: reply.reasoning,
      responseMetadata: reply.metadata,
    );
    _endTurn();
    unawaited(_deliver(message,
        elapsedMs: elapsed,
        toolCalls: List.unmodifiable(handoffTools),
        expectedGeneration: generation));
    for (final ev in autoHandoffs) {
      unawaited(_executeNonStreamHandoff(ev, generation));
    }
    _notify();
    onAnnounce?.call('Sonder replied');
    _afterTurn();
  }

  Future<void> _executeNonStreamHandoff(
      ChatStreamToolEvent ev, int generation) async {
    final execRes = await _backend.executeClientTool(
      callId: ev.callId,
      tool: ev.tool,
      args: ev.rawArgs,
      risk: ev.risk,
      mode: clientPermissionMode,
      workspaceRoot: clientWorkspaceRoot,
      approved: true,
      companionUrl: clientAgentUrl,
      companionToken: clientAgentToken,
    );
    if (_disposed || generation != _backendGeneration) return;
    if (execRes.companionUnavailable || execRes.needsApproval) {
      _updateEntryTool(
        ev.callId,
        (t) => t.copyWith(
          state: 'awaiting_client_approval',
          error: execRes.error.isEmpty ? null : execRes.error,
        ),
      );
      return;
    }
    final session = ev.session.isNotEmpty
        ? ev.session
        : sessionFor(_currentThreadId);
    try {
      await _backend.submitClientToolResult(
        ev.callId,
        ok: execRes.ok,
        output: execRes.output,
        error: execRes.error,
        denied: execRes.denied,
        session: session,
      );
      if (_disposed || generation != _backendGeneration) return;
      _updateEntryTool(
        ev.callId,
        (t) => t.copyWith(
          state: execRes.ok ? 'done' : 'error',
          phase: execRes.ok ? 'finished' : 'error',
          ok: execRes.ok,
          error: execRes.error.isEmpty ? null : execRes.error,
          clearError: execRes.error.isEmpty,
          result: ToolRunResult(ok: execRes.ok, output: execRes.output),
        ),
      );
    } catch (e) {
      if (_disposed || generation != _backendGeneration) return;
      _updateEntryTool(
        ev.callId,
        (t) => t.copyWith(state: 'error', error: '$e'),
      );
    }
  }

  void _finishError(ChatTurn turn, Object e, {int? expectedGeneration}) {
    final generation = expectedGeneration ?? _backendGeneration;
    if (_disposed || generation != _backendGeneration) return;
    final elapsed = _liveElapsedMs();
    final toolCalls = live.value?.toolCalls ?? const <ChatStreamToolEvent>[];
    ChatMessage message;
    var retryable = false;
    if (e is SonderException && isWorkCapacityError(e)) {
      message = ChatMessage(
        role: Role.assistant,
        content: workCapacityText,
        error: true,
        diagnostic: [
          if (e.diagnosticText.isNotEmpty) e.diagnosticText,
          if (!e.diagnosticText.contains(workCapacityCode))
            'code: $workCapacityCode',
        ].join('\n'),
      );
      retryable = true;
    } else if (e is SonderException) {
      final diagnostics = <String>[
        if (e.diagnosticText.isNotEmpty) e.diagnosticText,
        if (verboseErrors && e.cause != null) 'cause: ${e.cause}',
      ];
      message = ChatMessage(
        role: Role.assistant,
        content: e.message,
        error: true,
        diagnostic: diagnostics.join('\n'),
      );
      // Transport failures and server-marked retryable errors can be
      // retried by a person; a policy refusal cannot.
      retryable = e.retryable || e.httpStatus == null;
    } else {
      message = ChatMessage(
        role: Role.assistant,
        content: 'Request failed: $e',
        error: true,
      );
      retryable = true;
    }
    _endTurn();
    unawaited(_deliver(message,
        retryable: retryable,
        elapsedMs: elapsed,
        toolCalls: toolCalls,
        expectedGeneration: generation));
    _notify();
    onAnnounce?.call('Request failed: ${message.content.split('\n').first}');
    _afterTurn();
  }

  void _afterTurn() {
    unawaited(pollStatus());
    // A turn can be what changed the mode.
    unawaited(refreshPermissionMode());
  }

  /// Stop the in-flight turn. Returns the user's text so the composer can
  /// offer it again. When it was the thread's first turn, the server session
  /// rotates so the cancelled turn cannot come back as history (P1-9).
  String? cancel() {
    final turn = _turn;
    if (turn == null) return null;
    turn.cancel();
    final threadId = _turnThreadId;
    final isCurrent = threadId == _currentThreadId;
    String? text;
    var firstTurn = false;
    if (isCurrent) {
      final userIndex = _indexOf(_userId);
      if (userIndex >= 0) {
        text = _entries[userIndex].message.content;
        firstTurn = !_entries
            .take(userIndex)
            .any((e) => !e.message.pending && !e.message.error);
      }
      _entries.removeWhere((e) => e.id == _pendingId || e.id == _userId);
    }
    _endTurn();
    if (firstTurn) unawaited(_rotateSession(threadId));
    if (isCurrent) unawaited(_saveThread(threadId, messages));
    _notify();
    return text;
  }

  /// Send the turn that produced error [entryId] again.
  Future<void> retry(int entryId) {
    if (sending) return Future.value();
    final i = _indexOf(entryId);
    if (i <= 0) return Future.value();
    final user = _entries[i - 1].message;
    if (user.role != Role.user) return Future.value();
    _entries.removeRange(i - 1, i + 1);
    _notify();
    return send(user.content);
  }

  // -- Work runs (P0-8) ----------------------------------------------------

  /// A work run finished: its persisted answer replaces the hand-off text in
  /// the same message, which is then saved.
  Future<void> resolveWorkRun(int entryId, WorkRun run) async {
    final i = _indexOf(entryId);
    if (i < 0) return;
    final old = _entries[i].message;
    final ChatMessage next;
    switch (run.status) {
      case 'returned':
      case 'refused':
        final acknowledgement = old.responseMetadata?.acknowledgement ?? '';
        final pendingRefusal = run.pendingRefusal;
        final parts = <String>[
          if (acknowledgement.isNotEmpty) acknowledgement,
          if (run.finalSummary.isNotEmpty) run.finalSummary,
          if (run.output.isNotEmpty) run.output,
          if (run.status == 'refused' &&
              run.finalSummary.isEmpty &&
              run.output.isEmpty &&
              pendingRefusal != null &&
              pendingRefusal.reason.isNotEmpty)
            pendingRefusal.reason,
        ];
        final isRefused = run.status == 'refused' || pendingRefusal != null;
        final nextMeta = old.responseMetadata != null
            ? old.responseMetadata!.withWork(
                workStatus: run.status,
                status: isRefused ? 'refused' : null,
                refusal: pendingRefusal,
              )
            : (isRefused
                ? ChatResponseMetadata(
                    workRunId: run.id,
                    workStatus: run.status,
                    status: 'refused',
                    refusal: pendingRefusal,
                  )
                : null);
        next = ChatMessage(
          role: Role.assistant,
          content: parts.isEmpty
              ? (isRefused
                  ? 'refused: work run ${run.id} was refused'
                  : '(empty response)')
              : parts.join('\n\n'),
          // The run is settled: keep the receipt, but it no longer marks a
          // running work run (workRunOf reads it first).
          responseMetadata: nextMeta,
        );
      default:
        final label = switch (run.status) {
          'cancelled' => 'was stopped',
          'budget_exceeded' => 'ran out of time',
          'interrupted' => 'was interrupted',
          'failed' => 'failed',
          _ => 'ended with an unknown outcome',
        };
        final acknowledgement = old.responseMetadata?.acknowledgement ?? '';
        final details = <String>[
          if (acknowledgement.isNotEmpty) acknowledgement,
          if (run.finalSummary.isNotEmpty) run.finalSummary,
          if (run.output.isNotEmpty) run.output,
        ];
        next = ChatMessage(
          role: Role.assistant,
          content: 'Work run ${run.id} $label.'
              '${details.isEmpty ? '' : '\n\n${details.join('\n\n')}'}',
          error: true,
          diagnostic: 'work run: ${run.id}\nstatus: ${run.status}',
        );
    }
    _entries[i] = _entries[i].withMessage(next);
    _notify();
    onAnnounce?.call(next.error ? 'Work run ended' : 'Sonder replied');
    await saveCurrentThread();
  }

  // -- Feedback --------------------------------------------------------

  Future<void> recordFeedback(String command) async {
    try {
      await _backend.recordFeedback(
        command,
        TurnRequest(
          history: const [],
          model: _model,
          contextSize: contextSize,
          sessionId: sessionFor(_currentThreadId),
          project: _project,
          allowApproximateLocation: allowApproximateLocation,
        ),
      );
    } catch (_) {
      // Passive learning never interrupts the chat.
    }
  }

  @override
  void dispose() {
    _discardActiveTurn();
    _disposed = true;
    _pollTimer?.cancel();
    _modeTimer?.cancel();
    _liveTimer?.cancel();
    status.dispose();
    connection.dispose();
    live.dispose();
    super.dispose();
  }
}

extension<T> on Iterable<T> {
  T? get firstOrNull {
    final it = iterator;
    return it.moveNext() ? it.current : null;
  }
}

/// A thread removed by [ChatController.deleteThread], kept in memory so the
/// delete can be undone.
class DeletedThread {
  final ChatThread thread;
  final int index;
  final bool wasCurrent;
  final int? _session;
  final String? _placeholderId;

  const DeletedThread._(this.thread, this.index, this.wasCurrent,
      this._session, this._placeholderId);
}
