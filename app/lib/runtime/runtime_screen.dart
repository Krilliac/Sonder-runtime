/// The Runtime workspace: one calm page per category (Overview, Activity,
/// Models, Permissions, …) on the kit's [CategoryScaffold], in the manner of
/// the Codex and Claude settings windows.
///
/// The screen owns the data and every action. Pages are `part`s that read
/// that state, so an action keeps running (and its outcome stays) when you
/// switch pages and come back. No action locks the page: each tracks its own
/// progress and shows its result next to the control that ran it.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../local_manager.dart';
import '../models.dart';
import '../settings.dart';
import '../theme.dart';
import '../ui/approval_sheet.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import '../ui/status_vocab.dart' show statusGlyphs;
import '../ui/strings.dart';
import '../workspace_ui.dart';
import 'approvals_panel.dart';
import 'host_tools_panel.dart';
import 'jobs_panel.dart';
import 'model_routing.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'runtime_rows.dart';
import 'status_word.dart';
import 'work_runs_panel.dart';

export 'runtime_data.dart' show RuntimeDataSource, HttpRuntimeDataSource;

part 'pages/overview_page.dart';
part 'pages/activity_page.dart';
part 'pages/models_page.dart';
part 'pages/permissions_page.dart';
part 'pages/memory_page.dart';
part 'pages/server_page.dart';
part 'pages/host_pages.dart';
part 'pages/developer_page.dart';
part 'panels/selfmod_panel.dart';
part 'panels/runtime_policy_panel.dart';
part 'panels/mcp_runtime_panel.dart';
part 'panels/learning_health_panel.dart';
part 'panels/context_health_panel.dart';
part 'panels/autopilot_panel.dart';
part 'panels/agent_status_panel.dart';
part 'panels/deployment_panel.dart';
part 'panels/operational_capabilities_panel.dart';
part 'panels/activity_panels.dart';
part 'panels/updates_panels.dart';
part 'panels/ecosystem_panel.dart';
part 'widgets.dart';

/// Former name, kept for existing call sites (`chat_screen.dart`, tests).
typedef SystemScreen = RuntimeScreen;

/// The Server page's "Local server" row: its text and whether it is ok.
///
/// [launcherDetected] requires the launcher's signed health identity
/// (`defaultServerReachable`) and stays the only proof of a launcher-managed
/// server. When the app is nonetheless connected and healthy on
/// 127.0.0.1:11435, the row says so instead of "Not detected", without
/// claiming the launcher manages it.
(String, bool) localServerRow({
  required bool launcherDetected,
  required String serverUrl,
  required bool connected,
}) {
  if (launcherDetected) return ('Reachable on 127.0.0.1:11435', true);
  final uri = Uri.tryParse(serverUrl.trim());
  final local = uri != null &&
      (uri.host == '127.0.0.1' || uri.host == 'localhost') &&
      uri.port == 11435;
  if (connected && local) {
    return ('Connected on 127.0.0.1:11435 (not launcher-managed)', true);
  }
  return ('Not detected on 127.0.0.1:11435', false);
}

/// The Runtime category ids, in rail order. Stable: deep links use them.
abstract final class RuntimeCategories {
  static const overview = 'overview';
  static const activity = 'activity';
  static const models = 'models';
  static const memory = 'memory';
  static const permissions = 'permissions';
  static const server = 'server';
  static const observatory = 'observatory';
  static const updates = 'updates';
  static const cluster = 'cluster';
  static const developer = 'developer';
  static const about = 'about';
}

class RuntimeScreen extends StatefulWidget {
  final Settings settings;
  final SystemInfo? initialInfo;
  final bool liveUpdates;
  final ValueChanged<WorkspaceDestination>? onNavigate;

  /// Work runs, approvals, jobs, fanout, compute and host tools. Defaults to
  /// direct HTTP reads of [settings]' server; tests pass a fake.
  final RuntimeDataSource? dataSource;

  /// Fixed clock for goldens; live screens use [DateTime.now].
  final DateTime? now;

  /// Opens the Observatory. Defaults to [LocalManager.launchObservatory]
  /// with [settings]' Observatory executable and web URL; tests pass a fake.
  final ObservatoryLauncher? observatoryLauncher;

  /// The category shown first ([RuntimeCategories]); Overview by default.
  final String? initialCategory;

  /// Opens the one mode-change flow (Chat's picker and raise sheet). When
  /// the app shell wires it, Permissions offers **Change mode…**; without
  /// it the page explains where the mode is changed.
  final VoidCallback? onChangePermissionMode;

  const RuntimeScreen({
    super.key,
    required this.settings,
    this.initialInfo,
    this.liveUpdates = true,
    this.onNavigate,
    this.dataSource,
    this.now,
    this.observatoryLauncher,
    this.initialCategory,
    this.onChangePermissionMode,
  });

  @override
  State<RuntimeScreen> createState() => _RuntimeScreenState();
}

/// One tracked action: running (with live progress), or its last outcome.
class _Tracked {
  final bool busy;
  final String? progress;
  final ActionOutcome? outcome;

  /// The launcher or local result behind a failure, for its startup log.
  final LocalActionResult? local;

  const _Tracked.busy([this.progress])
      : busy = true,
        outcome = null,
        local = null;

  const _Tracked.done(this.outcome, {this.local})
      : busy = false,
        progress = null;
}

/// Thrown after a tracked action failed, so its [AsyncActionButton] reads
/// "Failed"; the outcome under the control says why.
class _ActionFailed implements Exception {
  const _ActionFailed();
}

/// One command run from the Developer console, newest first.
class _ConsoleEntry {
  final int id;
  final String command;
  final DateTime at;
  final bool running;
  final ActionOutcome? outcome;

  const _ConsoleEntry({
    required this.id,
    required this.command,
    required this.at,
    this.running = true,
    this.outcome,
  });

  _ConsoleEntry done(ActionOutcome outcome) => _ConsoleEntry(
      id: id, command: command, at: at, running: false, outcome: outcome);
}

class _RuntimeScreenState extends State<RuntimeScreen>
    with WidgetsBindingObserver {
  final _consoleInput = TextEditingController(text: '/diagnostics');
  final _trainCount = TextEditingController(text: '10');
  final _autopilotGoal = TextEditingController();
  bool _autopilotObserve = false;
  bool _autopilotWeb = true;
  bool _autopilotAdaptive = true;
  String? _autopilotGoalError;
  SystemInfo? _info;
  UpdateStatus? _updateStatus;
  ExtensionRegistryStatus? _extensionRegistry;
  LocalInstallInfo? _localInfo;
  LauncherStatus? _launcherInfo;
  LauncherOperation? _launcherOperation;
  String _launcherError = '';
  bool _loading = false;
  bool _polling = false;
  bool _waitingForLauncherOperation = false;
  bool _stopLauncherWait = false;

  /// The person pressed Stop waiting: the end of the wait is a note, not a
  /// failure.
  bool _stoppedWaitingByUser = false;

  /// A launcher action this page started is in flight; polls must not start
  /// a second follower for its operation.
  bool _launcherActionInFlight = false;
  bool _appActive = true;
  int _launcherActionEpoch = 0;
  String _ignoredLauncherOperationId = '';
  Timer? _pollTimer;
  int _pollCount = 0;

  /// True after a refresh or poll could not reach the server. The last
  /// loaded values stay on screen under an "as of" banner.
  bool _offline = false;

  /// The server answered, but not with status (401, 403, 421, 5xx…).
  String? _serverError;

  /// True for transport failures, false when the server answered an HTTP
  /// error: "can't reach" and "refused" are different remedies.
  static bool _unreachable(Object error) {
    if (error is! SonderException) return true;
    if (error.httpStatus != null) return false;
    return !RegExp(r'HTTP \d{3}|Unauthorized').hasMatch(error.message);
  }

  DateTime? _lastInfoAt;
  List<WorkRun>? _workRuns;
  Object? _workRunsError;
  ApprovalsPage? _approvals;
  Object? _approvalsError;
  EcosystemReading? _ecosystem;

  /// `/v1/models` rows: routing labels when the ecosystem read is refused.
  ModelCatalog? _modelCatalog;
  Object? _ecosystemError;
  bool _loadingExtras = false;
  PermissionMode? _mode;
  Object? _modeError;
  bool _modeLoaded = false;

  /// Every tracked action by id ("server", "setup", "autopilot", …).
  final Map<String, _Tracked> _tracked = {};

  /// Which server lifecycle action is running: start, stop or restart.
  String _serverActionId = '';

  /// Work runs with a Stop request in flight: a second confirm while the
  /// first POST is pending must not send another one.
  final Set<String> _stopping = {};
  final Map<String, ActionOutcome> _runOutcomes = {};

  /// Approvals: call ids being checked, call ids being approved (after the
  /// sheet), nonces being revoked, and each one's outcome.
  final Set<String> _approvalBusy = {};
  final Set<String> _approving = {};
  final Map<String, ActionOutcome> _approvalOutcomes = {};

  /// The Developer console, newest first.
  final List<_ConsoleEntry> _console = [];
  int _consoleSeq = 0;
  static const _consoleKeep = 5;

  late RuntimeDataSource _data = _dataSourceFor(widget);

  RuntimeDataSource _dataSourceFor(RuntimeScreen screen) =>
      screen.dataSource ??
      HttpRuntimeDataSource(
        baseUrl: screen.settings.serverUrl,
        apiKey: screen.settings.apiKey,
        accountSession: screen.settings.accountSession,
      );

  /// One [SonderApi] per server identity, rebuilt when the settings change.
  late SonderApi _api = _apiFor(widget.settings);

  static SonderApi _apiFor(Settings s) => SonderApi(
        baseUrl: s.serverUrl,
        apiKey: s.apiKey,
        accountSession: s.accountSession,
      );

  SonderLauncherApi get _launcherApi => SonderLauncherApi(
        baseUrl: widget.settings.effectiveLauncherUrl,
        token: widget.settings.launcherToken,
      );

  DateTime get _now => widget.now ?? DateTime.now();

  @override
  void didUpdateWidget(covariant RuntimeScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.settings != widget.settings) {
      _api = _apiFor(widget.settings);
    }
    if (oldWidget.dataSource != widget.dataSource ||
        oldWidget.settings != widget.settings) {
      _data = _dataSourceFor(widget);
      _workRuns = null;
      _workRunsError = null;
      _approvals = null;
      _approvalsError = null;
      _approvalOutcomes.clear();
      _ecosystem = null;
      _modelCatalog = null;
      _ecosystemError = null;
      _mode = null;
      _modeError = null;
      _modeLoaded = false;
      unawaited(_loadExtras());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stopLauncherWait = true;
    _launcherActionEpoch += 1;
    _consoleInput.dispose();
    _trainCount.dispose();
    _autopilotGoal.dispose();
    _pollTimer?.cancel();
    super.dispose();
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _info = widget.initialInfo;
    if (_info != null) _lastInfoAt = widget.now ?? DateTime.now();
    if (widget.liveUpdates) {
      _refresh();
      _pollTimer = Timer.periodic(
        const Duration(seconds: 2),
        (_) => _pollSystemInfo(),
      );
    } else if (widget.dataSource != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _loadExtras();
      });
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _appActive = true;
      if (widget.liveUpdates && !_loading) unawaited(_refresh());
      return;
    }
    _appActive = false;
    _stopLauncherWait = true;
    _launcherActionEpoch += 1;
    if (_waitingForLauncherOperation && mounted) {
      setState(() => _waitingForLauncherOperation = false);
    }
  }

  // -- Reads ---------------------------------------------------------------

  /// Work runs, approvals, the ecosystem status, the model catalog and the
  /// permission mode: the reads beyond `/v1/sonder/status` on each cycle.
  Future<void> _loadExtras() async {
    if (_loadingExtras) return;
    setState(() => _loadingExtras = true);
    List<WorkRun>? runs;
    Object? runsError;
    ApprovalsPage? approvals;
    Object? approvalsError;
    EcosystemReading? ecosystem;
    Object? ecosystemError;
    ModelCatalog? catalog;
    PermissionMode? mode;
    Object? modeError;
    Future<void> readRuns() async {
      try {
        runs = await _data.workRuns();
      } catch (error) {
        runsError = error;
      }
    }

    Future<void> readApprovals() async {
      try {
        approvals = await _data.approvals();
      } catch (error) {
        approvalsError = error;
      }
    }

    Future<void> readEcosystem() async {
      try {
        ecosystem = await _data.ecosystem();
      } catch (error) {
        ecosystemError = error;
      }
    }

    Future<void> readCatalog() async {
      try {
        catalog = await _data.modelCatalog();
      } catch (_) {
        // Only labels depend on it; a failed read keeps the last one.
      }
    }

    Future<void> readMode() async {
      try {
        mode = await _data.permissionMode();
      } catch (error) {
        modeError = error;
      }
    }

    await Future.wait([
      readRuns(),
      readApprovals(),
      readEcosystem(),
      readCatalog(),
      readMode(),
    ]);
    if (!mounted) return;
    setState(() {
      _loadingExtras = false;
      _workRuns = runs ??
          (runsError is SonderException &&
                  (runsError as SonderException).httpStatus == 403
              ? null
              : _workRuns);
      _workRunsError = runsError;
      _approvals = approvals ?? _approvals;
      _approvalsError = approvalsError;
      // A refused read drops what an earlier key could see; a transport
      // failure keeps the last reading beside the error.
      final refused = ecosystemError is SonderException &&
          const {401, 403}
              .contains((ecosystemError as SonderException).httpStatus);
      _ecosystem = ecosystem ?? (refused ? null : _ecosystem);
      _modelCatalog = catalog ?? _modelCatalog;
      _ecosystemError = ecosystemError;
      // A mode that cannot be read is never shown as if it were current.
      _mode = modeError == null ? (mode?.isUsable == true ? mode : null) : null;
      _modeError = modeError;
      _modeLoaded = true;
    });
  }

  /// Polls only while this page is the visible route: a route pushed on
  /// top (or a dialog) pauses them, like a backgrounded app does.
  bool get _visible {
    if (!mounted) return false;
    final route = ModalRoute.of(context);
    return route == null || route.isCurrent;
  }

  Future<void> _pollSystemInfo() async {
    if (!mounted || !_appActive || _loading || _polling) return;
    if (!_visible) return;
    _polling = true;
    SystemInfo? info;
    LauncherStatus? launcherInfo;
    try {
      if (widget.settings.usesHostLauncher) {
        try {
          launcherInfo = await _launcherApi.status();
        } catch (_) {
          // Launcher diagnostics are shown by the explicit Refresh path.
        }
      }
      final launcherStatus = launcherInfo;
      if (mounted && _appActive && launcherStatus != null) {
        setState(() => _recordLauncherStatus(launcherStatus));
        final activeOperation = launcherStatus.activeOperation;
        if (activeOperation != null) {
          _resumeLauncherOperation(activeOperation);
        }
      }
      var reached = true;
      try {
        info = await _api.systemInfo();
      } catch (error) {
        // The explicit Refresh path reports connection errors. Background
        // polls preserve the last useful snapshot under an "as of" banner.
        reached = !_unreachable(error);
      }
      if (mounted && _appActive) {
        setState(() {
          if (info != null) {
            _info = info;
            _lastInfoAt = DateTime.now();
          }
          _offline = !reached;
        });
        // Work runs change slowly; read them every fifth poll (10 s).
        if (reached && ++_pollCount % 5 == 0) unawaited(_loadExtras());
      }
    } catch (_) {
      // Keep polling best-effort so a sleeping host does not destabilize UI.
    } finally {
      _polling = false;
    }
  }

  Future<void> _refresh() async {
    setState(() => _loading = true);
    try {
      final localInfo = await LocalManager.inspect();
      SystemInfo? info;
      LauncherStatus? launcherInfo;
      String serverError = '';
      String launcherError = '';
      if (widget.settings.usesHostLauncher) {
        try {
          launcherInfo = await _launcherApi.status();
        } on SonderException catch (e) {
          launcherError = e.message;
        }
      }
      if (!mounted || !_appActive) return;
      final launcherStatus = launcherInfo;
      if (launcherStatus != null) {
        setState(() => _recordLauncherStatus(launcherStatus));
        final activeOperation = launcherStatus.activeOperation;
        if (activeOperation != null) {
          _resumeLauncherOperation(activeOperation);
        }
      }
      var unreachable = false;
      try {
        info = await _api.systemInfo();
      } on SonderException catch (e) {
        serverError = e.message;
        unreachable = _unreachable(e);
      }
      UpdateStatus? updateStatus;
      try {
        updateStatus = await _api.fetchUpdateStatus();
      } catch (_) {
        // Update status is best-effort: a non-admin key or older build
        // simply hides the category.
      }
      ExtensionRegistryStatus? extensionRegistry;
      try {
        extensionRegistry = await _api.fetchExtensionRegistry();
      } catch (_) {
        // Optional admin projection: older/non-admin servers hide it.
      }
      if (!mounted || !_appActive) return;
      if (info != null) unawaited(_loadExtras());
      setState(() {
        if (info != null) {
          _info = info;
          _lastInfoAt = DateTime.now();
        }
        _offline = unreachable;
        _serverError = info == null && !unreachable ? serverError : null;
        if (updateStatus != null) _updateStatus = updateStatus;
        if (extensionRegistry != null) _extensionRegistry = extensionRegistry;
        _localInfo = localInfo;
        _launcherError = launcherError;
      });
    } catch (e) {
      if (mounted) setState(() => _serverError = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  // -- Tracked actions -----------------------------------------------------

  bool _busy(String id) => _tracked[id]?.busy ?? false;

  void _dismiss(String id) {
    if (_tracked[id]?.busy == true) return;
    setState(() => _tracked.remove(id));
  }

  void _setProgress(String id, String progress) {
    if (!mounted || _tracked[id]?.busy != true) return;
    setState(() => _tracked[id] = _Tracked.busy(progress));
  }

  /// Runs [action] as the tracked action [id]: its control reads busy, then
  /// its outcome shows under it. Throws after a failure so an
  /// [AsyncActionButton] reads "Failed" (its onError swallows that).
  Future<void> _track(String id, Future<ActionOutcome> Function() action,
      {String? progress}) async {
    if (_busy(id)) return;
    setState(() => _tracked[id] = _Tracked.busy(progress));
    ActionOutcome outcome;
    try {
      outcome = await action();
    } on SonderException catch (error) {
      outcome = ActionOutcome.failed(error.message);
    } catch (error) {
      outcome = ActionOutcome.failed('$error');
    }
    if (!mounted) return;
    setState(() => _tracked[id] = _Tracked.done(outcome));
    if (outcome.kind == StatusKind.fail) throw const _ActionFailed();
  }

  /// A slash command sent as a chat message (no session), its reply as the
  /// outcome's raw output.
  Future<ActionOutcome> _command(String command, {String? title}) async {
    try {
      final reply = await _api.chat(
        [ChatMessage(role: Role.user, content: command)],
        model: widget.settings.model,
        contextSize: widget.settings.contextSize,
      );
      return ActionOutcome.ok(title ?? command, output: reply, word: 'done');
    } on SonderException catch (e) {
      return ActionOutcome.failed('$command failed', detail: e.message);
    }
  }

  /// Runs a slash command as the tracked action [id].
  Future<void> _trackCommand(String id, String command, {String? title}) =>
      _track(id, () => _command(command, title: title));

  /// The command a shared outcome slot last ran ("learning" → "/quality"),
  /// so only the button that pressed it reads busy.
  final Map<String, String> _commandOf = {};

  /// Runs [command] in the shared outcome slot [id].
  Future<void> _slotCommand(String id, String command) {
    setState(() => _commandOf[id] = command);
    return _trackCommand(id, command);
  }

  String? get _learningCommand => _commandOf['learning'];

  Future<void> _learningReport(String command) =>
      _slotCommand('learning', command);

  // -- Developer console ---------------------------------------------------

  Future<void> _runConsole(String command) async {
    final text = command.trim();
    if (text.isEmpty) return;
    final entry =
        _ConsoleEntry(id: ++_consoleSeq, command: text, at: DateTime.now());
    setState(() {
      _console.insert(0, entry);
      _trimConsole();
    });
    final outcome = await _command(text);
    if (!mounted) return;
    setState(() {
      final index = _console.indexWhere((e) => e.id == entry.id);
      if (index >= 0) _console[index] = _console[index].done(outcome);
    });
    if (outcome.kind == StatusKind.fail) throw const _ActionFailed();
  }

  /// A note in the console that no request was sent for [command].
  void _consoleNote(String command, ActionOutcome outcome) {
    setState(() {
      _console.insert(
          0,
          _ConsoleEntry(id: ++_consoleSeq, command: command, at: DateTime.now())
              .done(outcome));
      _trimConsole();
    });
  }

  void _trimConsole() {
    while (_console.length > _consoleKeep) {
      final index = _console.lastIndexWhere((e) => !e.running);
      if (index < 0) break;
      _console.removeAt(index);
    }
  }

  void _clearConsole() =>
      setState(() => _console.removeWhere((entry) => !entry.running));

  // -- Work runs -----------------------------------------------------------

  Future<void> _stopWorkRun(WorkRun run) async {
    if (!_stopping.add(run.id)) return;
    setState(() => _runOutcomes.remove(run.id));
    ActionOutcome outcome;
    try {
      await _data.cancelWorkRun(run.id);
      outcome = ActionOutcome.ok(
          'Stop requested for ${run.shortId}. Changes stop at the next step.',
          word: 'done');
    } on SonderException catch (error) {
      outcome = ActionOutcome.failed(error.httpStatus == 403
          ? 'Work runs need a developer or admin account.'
          : error.message);
    } on ArgumentError {
      // The id did not look like a work run id; nothing was sent.
      outcome =
          ActionOutcome.failed('Could not stop ${run.shortId}: unknown id.');
    } finally {
      _stopping.remove(run.id);
    }
    if (!mounted) return;
    setState(() => _runOutcomes[run.id] = outcome);
    await _loadExtras();
  }

  void _dismissRunOutcome(String id) => setState(() => _runOutcomes.remove(id));

  // -- Approvals -----------------------------------------------------------

  void _dismissApprovalOutcome(String key) =>
      setState(() => _approvalOutcomes.remove(key));

  /// Approve-once, exactly as UX-CONTRACT.md has it: read the server's
  /// pending entry again, draw the sheet from it, and only **Approve once**
  /// sends one POST bound to that entry's tool and digest.
  Future<void> _approve(PendingApproval item) async {
    final id = item.callId;
    if (_approvalBusy.contains(id) || _approving.contains(id)) return;
    setState(() {
      _approvalBusy.add(id);
      _approvalOutcomes.remove(id);
    });
    PendingApproval? call;
    ActionOutcome? failure;
    try {
      final fresh = await _data.approvals(limit: 200);
      if (!fresh.supported) {
        failure = _consoleApproval(id);
      } else {
        for (final pending in fresh.pending) {
          if (pending.callId == id) call = pending;
        }
        if (call == null) {
          failure = ActionOutcome.failed(
              'No refused call ${SonderStrings.shortCallId(id)} is waiting for '
              'approval on this server.',
              detail: 'It may already have run, been approved, or aged out.');
        }
      }
    } on SonderException catch (error) {
      failure = const {401, 403}.contains(error.httpStatus)
          ? const ActionOutcome(
              StatusKind.warn, SonderStrings.approvalsNeedRole)
          : ActionOutcome.failed('Could not check the call',
              detail: error.message);
    } finally {
      _approvalBusy.remove(id);
    }
    if (!mounted) return;
    if (failure != null || call == null) {
      setState(() => _approvalOutcomes[id] = failure!);
      unawaited(_loadExtras());
      return;
    }
    setState(() {});
    // The sheet opens once the check has finished, so the button is idle
    // while the person decides.
    unawaited(_openApprovalSheet(call));
  }

  ActionOutcome _consoleApproval(String callId) => ActionOutcome(
        StatusKind.note,
        SonderStrings.approveFromConsole,
        detail: 'This server cannot take approvals from the app. Run the '
            'command at the Sonder console on the PC.',
        output: SonderStrings.approveCommand(callId),
      );

  Future<void> _openApprovalSheet(PendingApproval call) async {
    final ttl = await showApprovalSheet(
      context,
      request: ApprovalRequest(
        tool: call.tool.isEmpty ? 'call' : call.tool,
        callId: call.callId,
        arguments:
            call.preview.isEmpty ? const [] : [('arguments', call.preview)],
        mode: call.mode.isEmpty ? 'the current' : call.mode,
        refusedAt: call.refusedAt == null ? null : clockLabel(call.refusedAt!),
      ),
    );
    if (ttl == null || !mounted) return;
    final id = call.callId;
    setState(() => _approving.add(id));
    ActionOutcome outcome;
    try {
      final issued = await _data.approveCall(id,
          ttl: ttl, tool: call.tool, digest: call.digest);
      final granted =
          issued.ttlSeconds > 0 ? Duration(seconds: issued.ttlSeconds) : ttl;
      outcome = ActionOutcome.ok(
          SonderStrings.approvalReceipt(call.tool.isEmpty ? 'call' : call.tool,
              id, issued.nonce, granted),
          word: 'approved');
    } on SonderException catch (error) {
      if (error.code == ApprovalsApi.unavailableCode) {
        outcome = _consoleApproval(id);
      } else if (const {401, 403}.contains(error.httpStatus)) {
        outcome = const ActionOutcome(
            StatusKind.warn, SonderStrings.approvalsNeedRole);
      } else {
        outcome = ActionOutcome.failed('The approval was not recorded',
            detail: error.message);
      }
    } finally {
      _approving.remove(id);
    }
    if (!mounted) return;
    setState(() => _approvalOutcomes[id] = outcome);
    await _loadExtras();
  }

  Future<void> _revoke(IssuedApproval item) async {
    final nonce = item.nonce;
    if (!_approvalBusy.add(nonce)) return;
    setState(() {
      _approvalOutcomes.remove(nonce);
      if (item.callId.isNotEmpty) _approvalOutcomes.remove(item.callId);
    });
    ActionOutcome outcome;
    try {
      await _data.revokeApproval(nonce);
      outcome = ActionOutcome.ok(
          'Revoked the approval for ${item.tool.isEmpty ? 'this call' : item.tool}.',
          word: 'done');
    } on SonderException catch (error) {
      outcome = error.code == ApprovalsApi.unavailableCode
          ? ActionOutcome(StatusKind.note, error.message)
          : ActionOutcome.failed('Could not revoke', detail: error.message);
    } finally {
      _approvalBusy.remove(nonce);
    }
    if (!mounted) return;
    setState(() => _approvalOutcomes[nonce] = outcome);
    await _loadExtras();
  }

  // -- Agents and autopilot ------------------------------------------------

  int get _activeAgents => _info?.agents?.activeAgents ?? 0;

  Future<bool?> _confirmCancelAgents() {
    final active = _activeAgents;
    if (active <= 0) return Future.value(true);
    return showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Cancel active agents?'),
        content: Text(
          'This cancels queued work at once and asks $active running '
          'agent${active == 1 ? '' : 's'} to stop. Model calls already '
          'running finish in the background; their late results are '
          'discarded.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Keep running'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Cancel agents'),
          ),
        ],
      ),
    );
  }

  static const _nothingToCancel = ActionOutcome(
      StatusKind.note, 'Nothing to cancel. No agents are running.');

  /// Cancel active (after its confirmation), with the outcome under the
  /// Agents card.
  Future<void> _cancelAgents() async {
    if (_activeAgents <= 0) {
      setState(() =>
          _tracked['agents-cancel'] = const _Tracked.done(_nothingToCancel));
      return;
    }
    await _trackCommand('agents-cancel', '/agentcancel all',
        title: 'Cancel requested for running agents');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh();
  }

  /// Cancel active from the Developer console.
  Future<void> _cancelAgentsFromConsole() async {
    if (_activeAgents <= 0) {
      _consoleNote('/agentcancel all', _nothingToCancel);
      return;
    }
    await _runConsole('/agentcancel all');
    if (mounted) unawaited(_refresh());
  }

  Future<bool?> _confirmRetryAgent(String agentId) => showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Retry interrupted work?'),
          content: Text(
            'Sonder Runtime reruns $agentId from its private restart-safe '
            'ledger, on the local code tier. Run /agentretry yourself to pick '
            'another tier.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Not now'),
            ),
            FilledButton.icon(
              onPressed: () => Navigator.pop(context, true),
              icon: const Icon(Icons.replay_outlined),
              label: const Text('Retry'),
            ),
          ],
        ),
      );

  Future<void> _retryAgent(String agentId) async {
    await _trackCommand('agent-retry:$agentId', '/agentretry $agentId',
        title: 'Retry requested for $agentId');
    if (mounted) await _refresh();
  }

  Future<void> _startAutopilot({required bool planOnly}) async {
    final objective = _autopilotGoal.text.trim();
    if (objective.isEmpty) {
      setState(() => _autopilotGoalError = 'Describe the goal first.');
      return;
    }
    setState(() => _autopilotGoalError = null);
    final options = <String>[
      if (_autopilotObserve) '--observe',
      if (!_autopilotWeb) '--no-web',
      if (!_autopilotAdaptive) '--static',
    ];
    final command = [
      '/autopilot',
      planOnly ? 'plan' : 'run',
      ...options,
      objective,
    ].join(' ');
    await _trackCommand('autopilot', command,
        title: planOnly ? 'Planning requested' : 'Goal started');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 300));
    if (mounted) await _refresh();
  }

  Future<bool?> _confirmCancelAutopilot(AutopilotRun run) => showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Cancel autonomous run?'),
          content: Text(
            'Cancel ${run.id}? A task already running may finish locally, '
            'but its result is discarded and the run cannot be resumed.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Keep running'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Cancel run'),
            ),
          ],
        ),
      );

  Future<void> _controlAutopilot(String action, AutopilotRun run) async {
    await _trackCommand('autopilot-control', '/autopilot $action ${run.id}',
        title: switch (action) {
          'resume' => 'Resume requested',
          'pause' => 'Pause requested',
          _ => 'Cancel requested',
        });
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh();
  }

  void _setAutopilotObserve(bool observe) =>
      setState(() => _autopilotObserve = observe);

  void _setAutopilotWeb(bool web) => setState(() => _autopilotWeb = web);

  void _setAutopilotAdaptive(bool adaptive) =>
      setState(() => _autopilotAdaptive = adaptive);

  void _clearGoalError() {
    if (_autopilotGoalError != null) {
      setState(() => _autopilotGoalError = null);
    }
  }

  /// Which composer button started the autopilot request in flight.
  String _autopilotAction = '';

  Future<void> _autopilotRequest(String action) async {
    setState(() => _autopilotAction = action);
    if (action == 'status') {
      await _trackCommand('autopilot', '/autopilot status',
          title: 'Autopilot status');
      return;
    }
    await _startAutopilot(planOnly: action == 'plan');
  }

  String _trainCommand() {
    final parsed = int.tryParse(_trainCount.text.trim()) ?? 10;
    final count = parsed.clamp(1, 500);
    return '/train $count';
  }

  // -- Server lifecycle ----------------------------------------------------

  bool get _localRuntimeControls =>
      LocalManager.canRunLocalTools && !widget.settings.hasHostLauncher;

  bool get _canControlServer =>
      _localRuntimeControls || widget.settings.usesHostLauncher;

  bool get _hostOperationActive =>
      _launcherOperation != null && !_launcherOperation!.isTerminal;

  void _recordLauncherStatus(LauncherStatus status) {
    _launcherInfo = status;
    _launcherError = '';
    final operation = status.currentOperation;
    if (operation != null) {
      _launcherOperation = operation;
    } else if (_launcherOperation != null &&
        !_launcherOperation!.isTerminal &&
        !_waitingForLauncherOperation) {
      // A previously active operation has left the launcher's active slot.
      // A locally followed operation records its terminal response directly;
      // otherwise clear the stale snapshot and allow another explicit action.
      _launcherOperation = null;
    }
  }

  void _resumeLauncherOperation(LauncherOperation operation) {
    if (!mounted ||
        !_appActive ||
        operation.isTerminal ||
        operation.id.isEmpty ||
        operation.id == _ignoredLauncherOperationId ||
        _waitingForLauncherOperation ||
        _launcherActionInFlight) {
      return;
    }
    unawaited(_followLauncherOperation(operation));
  }

  /// Follows a host operation this page did not start (another device, or
  /// before this page opened), with its progress under the server controls.
  Future<void> _followLauncherOperation(LauncherOperation operation) async {
    final actionEpoch = ++_launcherActionEpoch;
    _stopLauncherWait = false;
    _stoppedWaitingByUser = false;
    setState(() {
      _launcherOperation = operation;
      _waitingForLauncherOperation = true;
      _serverActionId = operation.action;
      _tracked['server'] = _Tracked.busy(operation.displayMessage);
    });
    ActionOutcome? outcome;
    try {
      final result = await _launcherApi.waitForOperation(
        operation.id,
        isCancelled: () =>
            !mounted ||
            !_appActive ||
            actionEpoch != _launcherActionEpoch ||
            _stopLauncherWait,
        onProgress: (status) {
          if (!mounted || actionEpoch != _launcherActionEpoch) return;
          setState(() => _recordLauncherStatus(status));
          final current = status.currentOperation;
          if (current != null) _setProgress('server', current.displayMessage);
        },
      );
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _recordLauncherStatus(result));
        final message = result.currentOperation?.displayMessage ??
            (result.message.isEmpty
                ? 'Host operation finished.'
                : result.message);
        final succeeded = result.currentOperation?.succeeded ?? result.ok;
        outcome = succeeded
            ? ActionOutcome.ok(message, word: 'done')
            : ActionOutcome.failed(message);
      }
    } on SonderException catch (error) {
      if (mounted && actionEpoch == _launcherActionEpoch) {
        outcome = _stoppedWaitingByUser
            ? _stoppedWaitingOutcome
            : ActionOutcome.failed('Host operation', detail: error.message);
      }
    } finally {
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() {
          _waitingForLauncherOperation = false;
          _tracked['server'] = _Tracked.done(outcome ??
              const ActionOutcome(
                  StatusKind.unknown, 'Stopped following the host operation.'));
        });
      }
    }
  }

  static const _stoppedWaitingOutcome = ActionOutcome(
    StatusKind.note,
    'Stopped waiting on this device.',
    detail: 'The host operation is not cancelled and may still be running.',
  );

  Future<LocalActionResult> _launcherAction(String action) async {
    if (!widget.settings.usesHostLauncher) {
      return const LocalActionResult(
        false,
        'Configure a valid explicit host launcher URL and token in Settings first.',
      );
    }
    final actionEpoch = ++_launcherActionEpoch;
    _stopLauncherWait = false;
    _stoppedWaitingByUser = false;
    _ignoredLauncherOperationId = '';
    _launcherActionInFlight = true;
    try {
      final result = await _launcherApi.action(
        action,
        contextSize: widget.settings.contextSize,
        isCancelled: () =>
            !mounted ||
            !_appActive ||
            actionEpoch != _launcherActionEpoch ||
            _stopLauncherWait,
        onProgress: (status) {
          if (!mounted || actionEpoch != _launcherActionEpoch) return;
          final operation = status.currentOperation;
          setState(() {
            _recordLauncherStatus(status);
            _waitingForLauncherOperation =
                operation != null && !operation.isTerminal;
          });
          if (operation != null) {
            _setProgress('server', operation.displayMessage);
          }
        },
      );
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _recordLauncherStatus(result));
      }
      return LocalActionResult(
        result.ok,
        result.message.isNotEmpty
            ? result.message
            : 'Host launcher $action completed.',
      );
    } on SonderException catch (e) {
      return LocalActionResult(false, e.message);
    } finally {
      _launcherActionInFlight = false;
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _waitingForLauncherOperation = false);
      }
    }
  }

  void _stopWaitingForLauncherAction() {
    if (!_waitingForLauncherOperation) return;
    setState(() {
      _stopLauncherWait = true;
      _stoppedWaitingByUser = true;
      _ignoredLauncherOperationId = _launcherOperation?.id ?? '';
      if (_busy('server')) {
        _tracked['server'] = const _Tracked.busy('Stopping this wait…');
      }
    });
  }

  Future<LocalActionResult> _startServer() {
    if (widget.settings.usesHostLauncher) return _launcherAction('start');
    if (widget.settings.hasHostLauncher) return _launcherAction('start');
    if (LocalManager.canRunLocalTools) {
      return LocalManager.startServer(
        allowHosted: widget.settings.allowHosted,
        contextSize: widget.settings.contextSize,
        persistOnAppClose: widget.settings.keepServerRunning,
      );
    }
    return _launcherAction('start');
  }

  Future<LocalActionResult> _stopServer() {
    if (widget.settings.usesHostLauncher) return _launcherAction('stop');
    if (widget.settings.hasHostLauncher) return _launcherAction('stop');
    if (LocalManager.canRunLocalTools) return LocalManager.stopServers();
    return Future.value(const LocalActionResult(
      false,
      'Configure an explicit host launcher URL in Settings first.',
    ));
  }

  Future<LocalActionResult> _restartServer() => _launcherAction('restart');

  /// Runs a local or launcher action as the tracked action [id]. A failure
  /// keeps its result so the startup log can be read inline, under the
  /// control, instead of in a blocking dialog.
  Future<void> _runLocal(
      String id, String label, Future<LocalActionResult> Function() action,
      {String? progress}) async {
    if (_busy(id)) return;
    setState(() => _tracked[id] = _Tracked.busy(progress));
    LocalActionResult result;
    try {
      result = await action();
    } catch (error) {
      result = LocalActionResult(false, '$error');
    }
    if (!mounted) return;
    final stopped = !result.ok && _stoppedWaitingByUser && id == 'server';
    final ActionOutcome outcome;
    if (result.ok) {
      outcome = ActionOutcome.ok(
          result.message.isEmpty ? '$label finished.' : result.message,
          word: 'done');
    } else if (stopped) {
      outcome = _stoppedWaitingOutcome;
    } else {
      outcome = ActionOutcome.failed('$label failed');
    }
    setState(() => _tracked[id] =
        _Tracked.done(outcome, local: result.ok || stopped ? null : result));
    if (result.ok) {
      await Future<void>.delayed(const Duration(seconds: 1));
      if (mounted) unawaited(_refresh());
      return;
    }
    if (!stopped) throw const _ActionFailed();
  }

  Future<void> _serverLifecycle(String actionId) {
    final (label, run) = switch (actionId) {
      'start' => ('Start server', _startServer),
      'stop' => ('Stop server', _stopServer),
      _ => ('Restart server', _restartServer),
    };
    setState(() => _serverActionId = actionId);
    return _runLocal('server', label, run);
  }

  // -- Observatory and copy ------------------------------------------------

  Future<ObservatoryLaunchResult> _launchObservatory(List<String> urls) {
    final custom = widget.observatoryLauncher;
    if (custom != null) return custom(urls);
    return LocalManager.launchObservatory(
      urls,
      runtimeUrl: widget.settings.serverUrl,
      executable: widget.settings.observatoryExecutable,
      webUrl: widget.settings.observatoryWebUrl,
    );
  }

  bool get _usesCredential =>
      widget.settings.apiKey.trim().isNotEmpty ||
      widget.settings.accountSession?.matches(widget.settings.serverUrl) ==
          true;

  // -- Badges --------------------------------------------------------------

  /// Work runs, agents and autopilot runs that are running now.
  int get _runningCount {
    final runs = _workRuns?.where((run) => run.isRunning).length ?? 0;
    final agents = _info?.agents?.activeAgents ?? 0;
    final autopilot = _info?.autopilot?.activeRuns ?? 0;
    return runs + agents + autopilot;
  }

  int get _approvalsWaiting => _approvals?.pending.length ?? 0;

  /// The Server rail badge: a problem with the server or its launcher.
  StatusKind? get _serverProblem {
    if (_offline || _serverError != null) return StatusKind.fail;
    if (_launcherInfo?.serverState == 'foreign_listener') {
      return StatusKind.warn;
    }
    final failed = _tracked['server']?.outcome?.kind == StatusKind.fail;
    return failed ? StatusKind.warn : null;
  }

  bool get _hasUpdates => _updateStatus != null || _extensionRegistry != null;

  // -- Layout --------------------------------------------------------------

  Widget? _banner() {
    if (!_offline && _serverError == null) return null;
    final host = serverLabel(widget.settings.serverUrl);
    final asOf = _offline && _info != null && _lastInfoAt != null
        ? 'Showing the last values, ${SonderStrings.asOf(clockLabel(_lastInfoAt!))}.'
        : null;
    return WorkspaceNotice(
      key: const Key('runtime-stale'),
      kind: StatusKind.fail,
      title: _offline ? "Can't reach $host" : _serverError!,
      detail: asOf,
      actions: [
        TextButton(
          onPressed: _loading ? null : _refresh,
          child: const Text(SonderStrings.retry),
        ),
      ],
    );
  }

  List<SonderCategory> _categories() {
    final running = _runningCount;
    final waiting = _approvalsWaiting;
    final problem = _serverProblem;
    return [
      SonderCategory(
        id: RuntimeCategories.overview,
        label: 'Overview',
        icon: Icons.space_dashboard_outlined,
        description: 'Health of this runtime at a glance.',
        keywords: const ['health', 'status', 'recent activity'],
        builder: (context) => _OverviewPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.activity,
        label: 'Activity',
        icon: Icons.bolt_outlined,
        description: 'Work runs, agents, autopilot and live execution.',
        badge: running > 0
            ? CountBadge(running, semantic: '$running running')
            : null,
        keywords: const [
          'work runs',
          'agents',
          'fleet',
          'autopilot',
          'goal',
          'live execution',
          'workbench',
          'jobs',
          'stop',
          'cancel',
        ],
        builder: (context) => _ActivityPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.models,
        label: 'Models',
        group: 'Inference',
        icon: Icons.memory_outlined,
        description: 'Routes, local aliases, context and providers.',
        keywords: const [
          'routes',
          'aliases',
          'lanes',
          'runtime policy',
          'context',
          'providers',
          'sonder inference',
          'ollama',
          'inference pool',
          'workers',
          'fanout',
        ],
        builder: (context) => _ModelsPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.memory,
        label: 'Memory & learning',
        group: 'Inference',
        icon: Icons.school_outlined,
        description:
            'What the runtime has learned, and how well it is grounded.',
        keywords: const [
          'learning quality',
          'lessons',
          'memory tiers',
          'improvements',
          'stats',
          'self-improvement',
          'selfmod',
          'practice',
          'train',
        ],
        builder: (context) => _MemoryPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.permissions,
        label: 'Permissions',
        group: 'Safety',
        icon: Icons.shield_outlined,
        description:
            'Approvals, the permission mode, and the tools agents can reach.',
        badge: waiting > 0
            ? Semantics(
                label: '$waiting approval${waiting == 1 ? '' : 's'} waiting',
                child: ExcludeSemantics(
                  child: StatusPill(StatusKind.warn,
                      word: '$waiting', dense: true),
                ),
              )
            : null,
        keywords: const [
          'approvals',
          'approve',
          'revoke',
          'permission mode',
          'host tools',
          'developer tools',
          'mcp',
        ],
        builder: (context) => _PermissionsPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.server,
        label: 'Server',
        group: 'Host',
        icon: Icons.dns_outlined,
        description: 'The local server process and its launcher.',
        badge: problem == null
            ? null
            : StatusPill(problem,
                word: problem == StatusKind.fail ? 'error' : 'warn',
                dense: true),
        keywords: const [
          'start',
          'stop',
          'restart',
          'launcher',
          'install',
          'setup',
          'update from git',
          'database',
          'state',
        ],
        builder: (context) => _ServerPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.observatory,
        label: 'Observatory',
        group: 'Host',
        icon: Icons.insights_outlined,
        description: 'Live telemetry for the Sonder Observatory.',
        keywords: const ['telemetry', 'export', 'connect urls'],
        builder: (context) => _ObservatoryPage(this),
      ),
      if (_hasUpdates)
        SonderCategory(
          id: RuntimeCategories.updates,
          label: 'Updates & extensions',
          group: 'Host',
          icon: Icons.system_update_alt_outlined,
          description: 'Releases of this runtime and its extensions.',
          keywords: const ['release', 'rollback', 'extensions', 'version'],
          builder: (context) => _UpdatesPage(this),
        ),
      SonderCategory(
        id: RuntimeCategories.cluster,
        label: 'Cluster',
        group: 'Host',
        icon: Icons.lan_outlined,
        description:
            'Deployment profile, distributed capabilities and compute nodes.',
        keywords: const [
          'deployment',
          'profile',
          'takeover',
          'failback',
          'capabilities',
          'compute nodes',
          'peers',
        ],
        builder: (context) => _ClusterPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.developer,
        label: 'Developer',
        group: 'Advanced',
        icon: Icons.terminal_outlined,
        description: 'Slash commands and the raw status report.',
        keywords: const [
          'command',
          'console',
          'slash',
          'quick commands',
          'raw status',
          'diagnostics',
        ],
        builder: (context) => _DeveloperPage(this),
      ),
      SonderCategory(
        id: RuntimeCategories.about,
        label: 'About',
        group: 'Advanced',
        icon: Icons.info_outline,
        description: 'Version, and how the runtime fits together.',
        keywords: const ['version', 'commit', 'architecture'],
        builder: (context) => _AboutPage(this),
      ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final shell = ShellScope.maybeOf(context);
    final refresh = IconButton(
      key: const Key('runtime-refresh'),
      tooltip: 'Refresh',
      onPressed: _loading ? null : _refresh,
      icon: _loading
          ? const SizedBox(
              key: Key('runtime-refreshing'),
              width: 18,
              height: 18,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : const Icon(Icons.refresh),
    );
    final Widget? leading;
    final List<Widget> actions;
    if (shell != null) {
      // The shell owns navigation: no way back to Chat here, only the menu
      // that opens the shell's drawer on narrow layouts.
      leading = shell.sidebarVisible
          ? null
          : IconButton(
              tooltip: 'Open navigation',
              onPressed: shell.openNavigation,
              icon: const Icon(Icons.menu),
            );
      actions = [refresh];
    } else {
      leading = IconButton(
        tooltip: 'Back to chat',
        onPressed: () => Navigator.of(context).maybePop(),
        icon: const Icon(Icons.arrow_back),
      );
      actions = [
        if (widget.onNavigate != null)
          WorkspaceMenu(
              current: WorkspaceDestination.runtime,
              onSelected: widget.onNavigate!),
        // No Tooltip wrapper: the visible "Chat" label carries the
        // affordance, and a hover tooltip collided with the window's Close.
        TextButton.icon(
          onPressed: () => Navigator.of(context).maybePop(),
          icon: const Icon(Icons.chat_bubble_outline, size: 18),
          label: const Text('Chat'),
        ),
        refresh,
      ];
    }
    return CategoryScaffold(
      title: 'Runtime',
      categories: _categories(),
      initialId: widget.initialCategory,
      leading: leading,
      actions: actions,
      banner: _banner(),
      navigationKey: const Key('runtime-nav'),
      searchHint: 'Search runtime',
    );
  }
}
