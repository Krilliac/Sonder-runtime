import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart' show AppLifecycleState;

import '../api.dart';
import '../chat_store.dart';
import '../models.dart';
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

  const ChatEntry(this.id, this.message,
      {this.retryable = false, this.elapsedMs});

  ChatEntry withMessage(ChatMessage next, {bool? retryable, int? elapsedMs}) =>
      ChatEntry(id, next,
          retryable: retryable ?? this.retryable,
          elapsedMs: elapsedMs ?? this.elapsedMs);
}

/// What the in-flight turn is doing, for the live line.
class LiveTurn {
  final DateTime startedAt;
  final String phase;
  final String model;
  final int? tokensIn;

  /// Whole seconds since the turn started. Advanced by the controller's
  /// 1 Hz tick and never behind the wall clock, so it is right after the app
  /// was suspended and testable under fake time.
  final int elapsedSeconds;

  const LiveTurn({
    required this.startedAt,
    this.phase = 'routing',
    this.model = '',
    this.tokensIn,
    this.elapsedSeconds = 0,
  });

  LiveTurn copyWith({String? phase, int? tokensIn, int? elapsedSeconds}) =>
      LiveTurn(
        startedAt: startedAt,
        phase: phase ?? this.phase,
        model: model,
        tokensIn: tokensIn ?? this.tokensIn,
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

  bool verboseErrors = false;

  /// The tier of the last reply (`code`, `fast`…), for the status line.
  String lastTier = '';

  // -- Model and commands -------------------------------------------------
  String _model;
  String get model => _model;
  List<String> _models = const ['sonder'];
  List<String> get models => _models;
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

  // ------------------------------------------------------------------------

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  /// Load threads and start the polls.
  Future<void> start() async {
    if (_started) return;
    _started = true;
    unawaited(refreshModels());
    unawaited(refreshCommands());
    unawaited(pollStatus());
    unawaited(refreshPermissionMode());
    await loadThreads();
  }

  /// Point the controller at a different server/key/account. Resets
  /// everything that belonged to the old one.
  void updateBackend(ChatBackend next, {required bool identityChanged}) {
    _backend = next;
    if (identityChanged) {
      _modeReadOnly = false;
      _mode = null;
      _lastKnownMode = null;
      connection.value = ConnectionStatus.connecting(next.serverUrl);
      status.value = null;
      _notify();
      unawaited(pollStatus());
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
    try {
      final info = await _backend.systemInfo();
      if (_disposed) return;
      status.value = info;
      _setConnection(ConnectionStatus(
          ConnState.connected, ConnectionStatus.hostOf(_backend.serverUrl)));
    } catch (e) {
      if (_disposed) return;
      status.value = null;
      _setConnection(ConnectionStatus.fromError(e, _backend.serverUrl));
    } finally {
      _pollInFlight = false;
      _schedulePoll();
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
      refreshPermissionMode(),
      refreshModels(),
    ]);
  }

  // -- Models and commands ---------------------------------------------------

  Future<void> refreshModels() async {
    try {
      final models = await _backend.listModels();
      if (_disposed || models.isEmpty) return;
      _models = models;
      _model = resolveCatalogModel(_models, _model);
      _notify();
    } catch (_) {
      // Offline / no auth: keep the fallback list.
    }
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
    try {
      final next = await _backend.fetchCommands();
      if (_disposed || next.isEmpty) return;
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
    PermissionMode? next;
    try {
      final mode = await _backend.fetchPermissionMode();
      next = (mode != null && mode.isUsable) ? mode : null;
    } catch (_) {
      next = null;
    } finally {
      _modeInFlight = false;
      _scheduleModePoll();
    }
    if (_disposed) return;
    if (next != null) _lastKnownMode = next;
    if (next?.mode == _mode?.mode &&
        next?.elevated == _mode?.elevated &&
        next?.elevationReason == _mode?.elevationReason &&
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
    if (isModeRaise(current.mode, target)) {
      final ok = await confirm(current.mode, target);
      if (!ok || _disposed) return (ModeChangeOutcome.declined, '');
    }
    _switchingMode = true;
    _notify();
    try {
      final next = await _backend.setPermissionMode(target);
      if (_disposed) return (ModeChangeOutcome.changed, '');
      _mode = next.isUsable ? next : null;
      if (_mode != null) _lastKnownMode = _mode;
      return (ModeChangeOutcome.changed, '');
    } on SonderException catch (e) {
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
      _mode = null;
      unawaited(refreshPermissionMode());
      return (ModeChangeOutcome.failed, 'Could not change mode.');
    } finally {
      _switchingMode = false;
      _notify();
    }
  }

  /// The mode Shift+Tab would move to, or null.
  String? nextModeInCycle() {
    final current = _mode;
    if (current == null || current.options.isEmpty) return null;
    final index = current.options.indexWhere((o) => o.name == current.mode);
    return current.options[(index + 1) % current.options.length].name;
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

  Future<void> deleteThread(ChatThread thread) async {
    if (sending && _turnThreadId == thread.id) cancel();
    final remaining = _threads.where((t) => t.id != thread.id).toList();
    final next =
        remaining.isEmpty ? [ChatThread.fresh(project: _project)] : remaining;
    final current = thread.id == _currentThreadId ? next.first : currentThread;
    _threads = next;
    _currentThreadId = current.id;
    _project = current.project;
    _setEntries(current.messages);
    _sessions.remove(thread.id);
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
    live.value = LiveTurn(startedAt: DateTime.now(), model: _model);
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
      model: _model,
      contextSize: contextSize,
      sessionId: sessionFor(_currentThreadId),
      project: _project,
      allowApproximateLocation: allowApproximateLocation,
      historyMode: _rotated(_currentThreadId) ? 'client' : null,
    );
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
        if (!identical(_turn, turn)) return;
        switch (event) {
          case TurnPhase(:final phase, :final tokensIn):
            live.value = live.value?.copyWith(phase: phase, tokensIn: tokensIn);
          case TurnDelta(:final text):
            buffer.write(text);
            _updatePending(buffer.toString());
          case TurnDone(:final reply):
            _finishOk(turn, reply);
        }
      },
      onError: (Object e, StackTrace _) {
        if (identical(_turn, turn)) _finishError(turn, e);
      },
      onDone: () {
        // A stream that closed without a result (a dropped connection).
        if (identical(_turn, turn)) {
          _finishError(
              turn,
              SonderException(
                  'The connection closed before the reply finished.',
                  retryable: true));
        }
      },
      cancelOnError: true,
    );
    return done.future;
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
  void _restorePendingRow() {
    if (_entries.isNotEmpty && _entries.last.message.pending) return;
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
      {bool retryable = false, int? elapsedMs}) async {
    final threadId = _turnThreadId;
    if (threadId == _currentThreadId) {
      _replace(
          _pendingId,
          (e) => ChatEntry(e.id, message,
              retryable: retryable, elapsedMs: elapsedMs));
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

  void _finishOk(ChatTurn turn, ChatReply reply) {
    final elapsed = _liveElapsedMs();
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
    unawaited(_deliver(message, elapsedMs: elapsed));
    _notify();
    onAnnounce?.call('Sonder replied');
    _afterTurn();
  }

  void _finishError(ChatTurn turn, Object e) {
    final elapsed = _liveElapsedMs();
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
    unawaited(_deliver(message, retryable: retryable, elapsedMs: elapsed));
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
        next = ChatMessage(
          role: Role.assistant,
          content: run.output.isEmpty ? '(empty response)' : run.output,
          responseMetadata: old.responseMetadata,
        );
      default:
        final label = switch (run.status) {
          'cancelled' => 'was stopped',
          'budget_exceeded' => 'ran out of time',
          'interrupted' => 'was interrupted',
          'failed' => 'failed',
          _ => 'ended with an unknown outcome',
        };
        next = ChatMessage(
          role: Role.assistant,
          content: 'Work run ${run.id} $label.'
              '${run.output.isEmpty ? '' : '\n\n${run.output}'}',
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
    _disposed = true;
    _pollTimer?.cancel();
    _modeTimer?.cancel();
    _liveTimer?.cancel();
    _turn?.cancel();
    _turnSub?.cancel();
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
