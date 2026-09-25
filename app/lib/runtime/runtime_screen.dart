/// The Runtime workspace: one overview, then collapsible detail sections.
///
/// Split out of the former `system_screen.dart` (plan P2-10). The detail
/// panels are `part`s so their private widgets stay library-private.
library;

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../api.dart';
import '../local_manager.dart';
import '../models.dart';
import '../settings.dart';
import '../theme.dart';
import '../workspace_ui.dart';
import 'approvals_panel.dart';
import 'jobs_panel.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'work_runs_panel.dart';

export 'runtime_data.dart' show RuntimeDataSource, HttpRuntimeDataSource;

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
part 'widgets.dart';
part 'navigation.dart';
part 'panels/updates_panels.dart';

/// Former name, kept for existing call sites (`chat_screen.dart`, tests).
typedef SystemScreen = RuntimeScreen;

class RuntimeScreen extends StatefulWidget {
  final Settings settings;
  final SystemInfo? initialInfo;
  final bool liveUpdates;
  final ValueChanged<WorkspaceDestination>? onNavigate;

  /// Work runs, approvals, jobs, fanout and compute. Defaults to direct HTTP
  /// reads of [settings]' server; tests pass a fake.
  final RuntimeDataSource? dataSource;

  /// Fixed clock for goldens; live screens use [DateTime.now].
  final DateTime? now;

  const RuntimeScreen({
    super.key,
    required this.settings,
    this.initialInfo,
    this.liveUpdates = true,
    this.onNavigate,
    this.dataSource,
    this.now,
  });

  @override
  State<RuntimeScreen> createState() => _RuntimeScreenState();
}

class _RuntimeScreenState extends State<RuntimeScreen>
    with WidgetsBindingObserver {
  final _customCommand = TextEditingController(text: '/diagnostics');
  final _trainCount = TextEditingController(text: '10');
  final _autopilotGoal = TextEditingController();
  bool _autopilotObserve = false;
  bool _autopilotWeb = true;
  bool _autopilotAdaptive = true;
  SystemInfo? _info;
  UpdateStatus? _updateStatus;
  ExtensionRegistryStatus? _extensionRegistry;
  LocalInstallInfo? _localInfo;
  LauncherStatus? _launcherInfo;
  LauncherOperation? _launcherOperation;
  String _launcherError = '';
  String? _message;
  bool _loading = false;
  bool _working = false;

  /// Label of the runtime action currently in flight, or '' when idle. Drives
  /// the in-place busy state on the button that was pressed.
  String _busyAction = '';

  /// Last failed runtime action, kept on screen after its dialog is dismissed
  /// so the startup-log evidence stays reachable.
  LocalActionResult? _runtimeFailure;
  String _runtimeFailureLabel = '';
  bool _polling = false;
  bool _waitingForLauncherOperation = false;
  bool _stopLauncherWait = false;
  bool _appActive = true;
  int _launcherActionEpoch = 0;
  String _ignoredLauncherOperationId = '';
  Timer? _pollTimer;
  int _pollCount = 0;

  /// True after a refresh or poll could not reach the server. The last
  /// loaded values stay on screen, dimmed and labelled "as of".
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
  String? _notice;
  List<WorkRun>? _workRuns;
  Object? _workRunsError;
  ApprovalsPage? _approvals;
  Object? _approvalsError;
  bool _loadingExtras = false;

  static const _sectionSpecs = <(String, String, IconData)>[
    ('overview', 'Overview', Icons.space_dashboard_outlined),
    ('workruns', 'Work runs', Icons.pending_actions_outlined),
    ('approvals', 'Approvals', Icons.fact_check_outlined),
    ('agents', 'Agents', Icons.hub_outlined),
    ('models', 'Models', Icons.memory_outlined),
    ('learning', 'Learning', Icons.school_outlined),
    ('updates', 'Updates', Icons.extension_outlined),
    ('deployment', 'Deployment', Icons.lan_outlined),
    ('jobs', 'Jobs', Icons.work_history_outlined),
    ('actions', 'Actions', Icons.tune_outlined),
  ];
  final Map<String, GlobalKey> _sectionKeys = {
    for (final spec in _sectionSpecs) spec.$1: GlobalKey(),
  };
  late final List<_RuntimeDestination> _destinations = [
    for (final spec in _sectionSpecs)
      _RuntimeDestination(spec.$1, spec.$2, spec.$3, _sectionKeys[spec.$1]!),
  ];
  final Map<String, bool> _expanded = {};
  bool _detailsOpenByDefault = true;

  late RuntimeDataSource _data = _dataSourceFor(widget);

  RuntimeDataSource _dataSourceFor(RuntimeScreen screen) =>
      screen.dataSource ??
      HttpRuntimeDataSource(
        baseUrl: screen.settings.serverUrl,
        apiKey: screen.settings.apiKey,
        accountSession: screen.settings.accountSession,
      );

  @override
  void didUpdateWidget(covariant RuntimeScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.dataSource != widget.dataSource ||
        oldWidget.settings != widget.settings) {
      _data = _dataSourceFor(widget);
      _workRuns = null;
      _workRunsError = null;
      _approvals = null;
      _approvalsError = null;
      unawaited(_loadExtras());
    }
  }

  bool _isOpen(String id) => _expanded[id] ?? _detailsOpenByDefault;

  Widget _group(
    String id,
    String title, {
    String? summary,
    required List<Widget> children,
  }) =>
      _DetailsGroup(
        key: _sectionKeys[id],
        title: title,
        summary: summary,
        expanded: _isOpen(id),
        onChanged: (open) => setState(() => _expanded[id] = open),
        children: children,
      );

  void _jumpToId(String id) =>
      _jumpTo(_destinations.firstWhere((d) => d.id == id));

  /// Opens the section, then scrolls it into view on the next frame.
  void _jumpTo(_RuntimeDestination destination) {
    if (destination.id != 'overview' && !_isOpen(destination.id)) {
      setState(() => _expanded[destination.id] = true);
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final target = destination.key.currentContext;
      if (target == null || !mounted) return;
      Scrollable.ensureVisible(
        target,
        duration: MediaQuery.disableAnimationsOf(context)
            ? Duration.zero
            : const Duration(milliseconds: 260),
        curve: Curves.easeOutCubic,
        alignment: 0.02,
      );
    });
  }

  String? _agentsSummary(SystemInfo? info) {
    if (info == null) return null;
    final agents = info.agents?.activeAgents ?? 0;
    final autopilot = info.autopilot?.activeRuns ?? 0;
    final parts = [
      if (agents > 0) '$agents agent${agents == 1 ? '' : 's'} running',
      if (autopilot > 0) '$autopilot autopilot',
    ];
    return parts.isEmpty ? 'idle' : parts.join(' · ');
  }

  /// Work runs and approvals: the overview's two extra reads.
  Future<void> _loadExtras() async {
    if (_loadingExtras) return;
    setState(() => _loadingExtras = true);
    List<WorkRun>? runs;
    Object? runsError;
    ApprovalsPage? approvals;
    Object? approvalsError;
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

    await Future.wait([readRuns(), readApprovals()]);
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
    });
  }

  /// Work runs with a Stop request in flight: a second confirm while the
  /// first POST is pending must not send another one.
  final Set<String> _stopping = {};

  Future<void> _stopWorkRun(WorkRun run) async {
    if (!_stopping.add(run.id)) return;
    try {
      await _data.cancelWorkRun(run.id);
      if (!mounted) return;
      setState(() => _message =
          'Stop requested for ${run.shortId}. Changes stop at the next step.');
    } on SonderException catch (error) {
      if (!mounted) return;
      setState(() => _message = error.httpStatus == 403
          ? 'Work runs need a developer or admin account.'
          : error.message);
    } on ArgumentError {
      // The id did not look like a work run id; nothing was sent.
      if (!mounted) return;
      setState(() => _message = 'Could not stop ${run.shortId}: unknown id.');
    } finally {
      _stopping.remove(run.id);
    }
    if (mounted) await _loadExtras();
  }

  SonderApi get _api => SonderApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
        accountSession: widget.settings.accountSession,
      );

  SonderLauncherApi get _launcherApi => SonderLauncherApi(
        baseUrl: widget.settings.effectiveLauncherUrl,
        token: widget.settings.launcherToken,
      );

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stopLauncherWait = true;
    _launcherActionEpoch += 1;
    _customCommand.dispose();
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
      if (widget.liveUpdates && !_loading) {
        unawaited(_refresh(preserveMessage: true));
      }
      return;
    }
    _appActive = false;
    _stopLauncherWait = true;
    _launcherActionEpoch += 1;
    if (_waitingForLauncherOperation && mounted) {
      setState(() => _waitingForLauncherOperation = false);
    }
  }

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
        _working) {
      return;
    }
    unawaited(_followLauncherOperation(operation));
  }

  Future<void> _followLauncherOperation(LauncherOperation operation) async {
    final actionEpoch = ++_launcherActionEpoch;
    _stopLauncherWait = false;
    setState(() {
      _launcherOperation = operation;
      _waitingForLauncherOperation = true;
      _message = operation.displayMessage;
    });
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
          setState(() {
            _recordLauncherStatus(status);
            final current = status.currentOperation;
            if (current != null) _message = current.displayMessage;
          });
        },
      );
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() {
          _recordLauncherStatus(result);
          _message = result.currentOperation?.displayMessage ?? result.message;
        });
      }
    } on SonderException catch (error) {
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _message = error.message);
      }
    } finally {
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _waitingForLauncherOperation = false);
      }
    }
  }

  /// Polls only while this page is the visible route: a route pushed on
  /// top (or a dialog) pauses them, like a backgrounded app does.
  bool get _visible {
    if (!mounted) return false;
    final route = ModalRoute.of(context);
    return route == null || route.isCurrent;
  }

  Future<void> _pollSystemInfo() async {
    if (!mounted || !_appActive || _loading || _working || _polling) return;
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
        // polls preserve the last useful snapshot, dimmed as "as of".
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

  Future<void> _refresh({bool preserveMessage = false}) async {
    setState(() {
      _loading = true;
      _notice = null;
      if (!preserveMessage) _message = null;
    });
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
        // simply hides the section.
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
        if (!preserveMessage && serverError.isNotEmpty) {
          _message = serverError;
        }
      });
    } catch (e) {
      if (mounted) setState(() => _message = e.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<LocalActionResult> _launcherAction(String action) async {
    if (!widget.settings.usesHostLauncher) {
      return const LocalActionResult(
        false,
        'Configure a valid explicit host launcher URL and token in Settings first.',
      );
    }
    final actionEpoch = ++_launcherActionEpoch;
    _stopLauncherWait = false;
    _ignoredLauncherOperationId = '';
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
            if (operation != null) {
              _message = operation.displayMessage;
            }
          });
        },
      );
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() {
          _recordLauncherStatus(result);
        });
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
      if (mounted && actionEpoch == _launcherActionEpoch) {
        setState(() => _waitingForLauncherOperation = false);
      }
    }
  }

  void _stopWaitingForLauncherAction() {
    if (!_waitingForLauncherOperation) return;
    setState(() {
      _stopLauncherWait = true;
      _ignoredLauncherOperationId = _launcherOperation?.id ?? '';
      _message = 'Stopping this device\'s wait. The host operation is not '
          'cancelled and may still be running.';
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

  Future<void> _run(
    Future<LocalActionResult> Function() action, {
    String label = '',
  }) async {
    setState(() {
      _working = true;
      _busyAction = label;
      _message = null;
      _runtimeFailure = null;
      _runtimeFailureLabel = '';
    });
    final result = await action();
    if (!mounted) return;
    setState(() {
      _working = false;
      _busyAction = '';
      _message = result.message;
      _runtimeFailure = result.ok ? null : result;
      _runtimeFailureLabel = result.ok ? '' : label;
    });
    if (!result.ok) {
      // A failed launcher used to leave the page unchanged: the button
      // re-enabled, the status still read "Not detected", and the reason sat
      // in a log nothing read. Make the failure modal so it cannot be missed.
      await _showActionFailure(label, result);
      return;
    }
    await Future<void>.delayed(const Duration(seconds: 1));
    if (mounted) _refresh();
  }

  Future<void> _showActionFailure(
    String label,
    LocalActionResult result,
  ) async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(label.isEmpty ? 'Action failed' : '$label failed'),
        content: SizedBox(
          width: 520,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                SelectableText(result.message),
                if (result.logTail.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  Text(
                    'Startup log',
                    style: Theme.of(ctx).textTheme.labelLarge,
                  ),
                  const SizedBox(height: 6),
                  _OutputCard(text: result.logTail),
                ],
              ],
            ),
          ),
        ),
        actions: [
          if (result.logPath.isNotEmpty)
            TextButton.icon(
              onPressed: () => _copyLogPath(ctx, result.logPath),
              icon: const Icon(Icons.copy, size: 18),
              label: const Text('Copy log path'),
            ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Dismiss'),
          ),
        ],
      ),
    );
  }

  /// Copy the startup-log path without clobbering [_message], which is still
  /// holding the failure text the operator is reading.
  Future<void> _copyLogPath(BuildContext dialogContext, String path) async {
    await Clipboard.setData(ClipboardData(text: path));
    if (!dialogContext.mounted) return;
    ScaffoldMessenger.of(dialogContext).showSnackBar(
      const SnackBar(content: Text('Startup log path copied.')),
    );
  }

  Future<void> _sendCommand(String command) async {
    final text = command.trim();
    if (text.isEmpty) return;
    setState(() {
      _working = true;
      _message = null;
    });
    try {
      final reply = await _api.chat([
        ChatMessage(role: Role.user, content: text),
      ],
          model: widget.settings.model,
          contextSize: widget.settings.contextSize);
      if (mounted) setState(() => _message = reply);
    } on SonderException catch (e) {
      if (mounted) setState(() => _message = e.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  Future<void> _cancelActiveAgents() async {
    final active = _info?.agents?.activeAgents ?? 0;
    if (active <= 0) {
      // Not an error: an info notice, so the page does not read as failing.
      setState(() => _notice = 'Nothing to cancel. No agents are running.');
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Cancel active agents?'),
        content: Text(
          'This will cancel queued work immediately and request cancellation '
          'for $active running agent${active == 1 ? '' : 's'}. Active model '
          'calls finish in the background and their late results are discarded.',
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
    if (confirmed != true || !mounted) return;
    await _sendCommand('/agentcancel all');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh();
  }

  Future<void> _retryPersistedAgent(String agentId) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Retry interrupted work?'),
        content: Text(
          'Sonder Runtime will rerun $agentId from its private restart-safe ledger. '
          'Retries use the local code tier unless you run /agentretry manually '
          'with a different tier.',
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
    if (confirmed != true || !mounted) return;
    await _sendCommand('/agentretry $agentId');
    if (mounted) await _refresh();
  }

  Future<void> _startAutopilot({required bool planOnly}) async {
    final objective = _autopilotGoal.text.trim();
    if (objective.isEmpty) {
      setState(() => _message = 'Enter an autonomous goal first.');
      return;
    }
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
    await _sendCommand(command);
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 300));
    if (mounted) await _refresh(preserveMessage: true);
  }

  Future<void> _controlAutopilot(String action, AutopilotRun run) async {
    if (action == 'cancel') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Cancel autonomous run?'),
          content: Text(
            'Cancel ${run.id}? Any active task may finish locally, but its late '
            'result will be discarded and the run cannot be resumed.',
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
      if (confirmed != true || !mounted) return;
    }
    await _sendCommand('/autopilot $action ${run.id}');
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (mounted) await _refresh(preserveMessage: true);
  }

  String _trainCommand() {
    final parsed = int.tryParse(_trainCount.text.trim()) ?? 10;
    final count = parsed.clamp(1, 500);
    return '/train $count';
  }

  /// Swap a button's leading icon for a spinner while that specific action is
  /// running, so the press has visible feedback at the button itself.
  Widget _busyIcon(String label, Widget idleIcon) {
    if (_busyAction != label) return idleIcon;
    return const SizedBox(
      width: 18,
      height: 18,
      child: CircularProgressIndicator(strokeWidth: 2),
    );
  }

  Future<void> _copy(String text) async {
    await Clipboard.setData(ClipboardData(text: text));
    if (!mounted) return;
    setState(() => _message = 'Copied to clipboard.');
  }

  @override
  Widget build(BuildContext context) {
    final info = _info;
    final localInfo = _localInfo;
    final localRuntimeControls =
        LocalManager.canRunLocalTools && !widget.settings.hasHostLauncher;
    final canControlServer =
        localRuntimeControls || widget.settings.usesHostLauncher;
    final hostOperationActive =
        _launcherOperation != null && !_launcherOperation!.isTerminal;
    var launcherServerText = 'Unknown';
    if (_launcherInfo?.serverState == 'foreign_listener') {
      launcherServerText = 'Conflict: another service is listening on '
          '${_launcherInfo!.serverHost}:${_launcherInfo!.serverPort}';
    } else if (_launcherInfo?.serverRunning == true) {
      launcherServerText =
          'Running on ${_launcherInfo!.serverHost}:${_launcherInfo!.serverPort}';
    } else if (_launcherInfo != null) {
      launcherServerText = 'Stopped';
    }
    return Scaffold(
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: IconButton(
          tooltip: 'Back to chat',
          onPressed: () => Navigator.of(context).maybePop(),
          icon: const Icon(Icons.arrow_back),
        ),
        title: const Text('Runtime'),
        actions: [
          if (widget.onNavigate != null)
            WorkspaceMenu(
                current: WorkspaceDestination.runtime,
                onSelected: widget.onNavigate!),
          // No Tooltip wrapper here. The button already carries a visible
          // "Chat" label, so a hover tooltip only added a floating box in the
          // top-right corner, where it collided with the window's own Close
          // tooltip. The destination stays discoverable through the leading
          // back button's tooltip.
          TextButton.icon(
            onPressed: () => Navigator.of(context).maybePop(),
            icon: const Icon(Icons.chat_bubble_outline, size: 18),
            label: const Text('Chat'),
          ),
          IconButton(
            tooltip: 'Refresh',
            onPressed: _loading ? null : _refresh,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: LayoutBuilder(
        builder: (context, constraints) {
          final wide = constraints.maxWidth >= 900;
          // Phones open with only the Overview; wider layouts open every
          // Details group, so nothing is hidden where there is room.
          _detailsOpenByDefault = constraints.maxWidth >= 600;
          final destinations = _destinations;
          final content = ListView(
            key: const Key('runtime-scroll'),
            padding: const EdgeInsets.all(16),
            children: [
              if (!wide) ...[
                _SystemCompactNav(
                  destinations: destinations,
                  onSelect: _jumpTo,
                ),
                const SizedBox(height: 12),
              ],
              Column(
                key: _sectionKeys['overview'],
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text('Overview',
                      style: Theme.of(context).textTheme.titleSmall),
                  const SizedBox(height: 8),
                  RuntimeOverview(
                    rows: overviewRows(
                      serverUrl: widget.settings.serverUrl,
                      info: info,
                      offline: _offline,
                      serverError: _serverError,
                      loading: _loading,
                      workRuns: _workRuns,
                      workRunsError: _workRunsError,
                      approvals: _approvals,
                      now: widget.now,
                      onOpenWorkRuns: () => _jumpToId('workruns'),
                      onReviewApprovals: () => _jumpToId('approvals'),
                    ),
                    activity: recentActivity(info?.executionFeed),
                    staleSince: _offline && info != null ? _lastInfoAt : null,
                    onRetry: _offline ? () => _refresh() : null,
                    onAllActivity: () => _jumpToId('agents'),
                  ),
                ],
              ),
              if (_loading || _working) ...[
                const SizedBox(height: 16),
                const LinearProgressIndicator(),
              ],
              if (_message != null) ...[
                const SizedBox(height: 16),
                _OutputCard(text: _message!),
              ],
              const SizedBox(height: 12),
              _group(
                'workruns',
                _workRuns == null
                    ? 'Work runs'
                    : 'Work runs (${_workRuns!.length})',
                children: [
                  WorkRunsPanel(
                    runs: _workRuns,
                    error: _workRunsError,
                    loading: _loadingExtras,
                    now: widget.now,
                    onRefresh: _loadExtras,
                    onStop: _stopWorkRun,
                  ),
                ],
              ),
              _group(
                'approvals',
                'Approvals',
                children: [
                  ApprovalsPanel(page: _approvals, error: _approvalsError),
                ],
              ),
              _group(
                'agents',
                'Agents & autopilot',
                summary: _agentsSummary(info),
                children: [
                  _Section(
                    title: 'Autopilot',
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Give Sonder Runtime an outcome, then let its local planner build '
                          'a persistent checklist, execute one guarded task at a time, '
                          'validate the result, and pause safely when a budget or '
                          'decision boundary is reached.',
                          style: Theme.of(context).textTheme.bodyMedium,
                        ),
                        const SizedBox(height: 12),
                        TextField(
                          key: const Key('autopilot-goal'),
                          controller: _autopilotGoal,
                          enabled: !_working,
                          minLines: 2,
                          maxLines: 4,
                          decoration: const InputDecoration(
                            labelText: 'Autonomous goal',
                            hintText:
                                'Inspect this project, implement the missing feature, and run its tests',
                            alignLabelWithHint: true,
                            border: OutlineInputBorder(),
                          ),
                        ),
                        const SizedBox(height: 10),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          crossAxisAlignment: WrapCrossAlignment.center,
                          children: [
                            ChoiceChip(
                              label: const Text('Workspace'),
                              avatar: const Icon(Icons.edit_note_outlined,
                                  size: 18),
                              selected: !_autopilotObserve,
                              onSelected: _working
                                  ? null
                                  : (_) =>
                                      setState(() => _autopilotObserve = false),
                            ),
                            ChoiceChip(
                              label: const Text('Observe only'),
                              avatar: const Icon(Icons.visibility_outlined,
                                  size: 18),
                              selected: _autopilotObserve,
                              onSelected: _working
                                  ? null
                                  : (_) =>
                                      setState(() => _autopilotObserve = true),
                            ),
                            FilterChip(
                              label: const Text('Public web'),
                              avatar:
                                  const Icon(Icons.public_outlined, size: 18),
                              selected: _autopilotWeb,
                              onSelected: _working
                                  ? null
                                  : (value) =>
                                      setState(() => _autopilotWeb = value),
                            ),
                            FilterChip(
                              label: const Text('Adaptive review'),
                              avatar:
                                  const Icon(Icons.route_outlined, size: 18),
                              selected: _autopilotAdaptive,
                              onSelected: _working
                                  ? null
                                  : (value) => setState(
                                      () => _autopilotAdaptive = value),
                            ),
                            Tooltip(
                              message:
                                  'Autopilot never receives location consent, cloud tiers, delete, account, permission, or fleet controls.',
                              child: Icon(
                                Icons.shield_outlined,
                                size: 20,
                                color: Theme.of(context).colorScheme.primary,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 10),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            FilledButton.tonalIcon(
                              key: const Key('autopilot-plan'),
                              onPressed: _working
                                  ? null
                                  : () => _startAutopilot(planOnly: true),
                              icon: const Icon(Icons.account_tree_outlined),
                              label: const Text('Plan only'),
                            ),
                            FilledButton.icon(
                              key: const Key('autopilot-run'),
                              onPressed: _working
                                  ? null
                                  : () => _startAutopilot(planOnly: false),
                              icon: const Icon(Icons.rocket_launch_outlined),
                              label: const Text('Run goal'),
                            ),
                            OutlinedButton.icon(
                              onPressed: _working
                                  ? null
                                  : () => _sendCommand('/autopilot status'),
                              icon: const Icon(Icons.manage_search_outlined),
                              label: const Text('Status'),
                            ),
                          ],
                        ),
                        if (info?.autopilot != null) ...[
                          const SizedBox(height: 14),
                          const Divider(),
                          const SizedBox(height: 8),
                          _AutopilotPanel(
                            status: info!.autopilot!,
                            onResume: (run) => _controlAutopilot('resume', run),
                            onPause: (run) => _controlAutopilot('pause', run),
                            onCancel: (run) => _controlAutopilot('cancel', run),
                          ),
                        ],
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (info != null) ...[
                    if (info.agents != null) ...[
                      _Section(
                        title: 'Agents',
                        child: _AgentStatusPanel(
                          status: info.agents!,
                          onRetry: _retryPersistedAgent,
                        ),
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                  if (info == null) ...[
                    _Section(
                      title: 'Live execution',
                      child: LiveExecutionFeed(
                        feed: null,
                        offline: _message != null,
                      ),
                    ),
                    const SizedBox(height: 12),
                  ],
                  if (info != null) ...[
                    _Section(
                      title: 'Live execution',
                      child: LiveExecutionFeed(feed: info.executionFeed),
                    ),
                    const SizedBox(height: 12),
                    if (info.activity?.displayResponse != null) ...[
                      _Section(
                        title: 'Workbench activity',
                        child: WorkbenchActivityPanel(
                          response: info.activity!.displayResponse!,
                          totalToolCalls: info.activity!.totalToolCalls,
                        ),
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                ],
              ),
              _group(
                'models',
                'Models & inference pool',
                summary: info == null ? null : '${info.models.length} routes',
                children: [
                  if (info != null) ...[
                    _Section(
                      title: 'Inference models',
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text(
                            'Sonder Runtime routes requests to these providers. '
                            'Ollama hosts and runs the local model weights.',
                          ),
                          const SizedBox(height: 10),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: info.models
                                .map((m) => Chip(
                                      label: Text('${m.id} - ${m.ownedBy}'),
                                      avatar: Icon(
                                        m.ownedBy == 'cloud'
                                            ? Icons.cloud_outlined
                                            : Icons.memory_outlined,
                                        size: 18,
                                      ),
                                    ))
                                .toList(),
                          ),
                        ],
                      ),
                    ),
                  ],
                  if (info != null) ...[
                    if (info.runtimePolicy != null) ...[
                      _Section(
                        title: 'Local Runtime Policy',
                        child: _RuntimePolicyPanel(policy: info.runtimePolicy!),
                      ),
                      const SizedBox(height: 12),
                    ],
                    if (info.context != null) ...[
                      _Section(
                        title: 'Context health',
                        child: _ContextHealthPanel(health: info.context!),
                      ),
                      const SizedBox(height: 12),
                    ],
                    if (info.mcpRuntime != null) ...[
                      _Section(
                        title: 'Runtime Convergence',
                        child: _McpRuntimePanel(runtime: info.mcpRuntime!),
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                  if (info == null) const _OutputText('No status loaded yet.'),
                ],
              ),
              _group(
                'learning',
                'Learning & memory',
                children: [
                  if (info != null) ...[
                    if (info.learningHealth != null) ...[
                      _Section(
                        title: 'Learning Quality',
                        child:
                            _LearningHealthPanel(health: info.learningHealth!),
                      ),
                      const SizedBox(height: 12),
                    ],
                    _Section(
                      title: 'Memory & grounded learning',
                      child: _OutputText(info.learnTiers),
                    ),
                    const SizedBox(height: 12),
                    if (info.improvements.isNotEmpty) ...[
                      _Section(
                        title: 'Improvements',
                        child: _OutputText(info.improvements),
                      ),
                      const SizedBox(height: 12),
                    ],
                    _Section(title: 'Stats', child: _OutputText(info.stats)),
                    const SizedBox(height: 12),
                    if (info.selfmod != null) ...[
                      _Section(
                        title: 'Safe self-improvement',
                        child: _SelfmodPanel(info: info.selfmod!),
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                  if (info == null) const _OutputText('No status loaded yet.'),
                ],
              ),
              _group(
                'updates',
                'Updates & extensions',
                children: [
                  if (_updateStatus != null) ...[
                    _UpdateSection(status: _updateStatus!),
                    const SizedBox(height: 12),
                  ],
                  if (_extensionRegistry != null) ...[
                    _ExtensionRegistrySection(status: _extensionRegistry!),
                    const SizedBox(height: 12),
                  ],
                  if (_updateStatus == null && _extensionRegistry == null)
                    const _OutputText(
                        'This server did not report updates or extensions.'),
                ],
              ),
              _group(
                'deployment',
                'Deployment & capabilities',
                children: [
                  if (info != null) ...[
                    if (info.deployment != null) ...[
                      _Section(
                        title: 'Deployment profile',
                        child: _DeploymentPanel(
                          key: const Key('deployment-panel'),
                          info: info.deployment!,
                        ),
                      ),
                      const SizedBox(height: 12),
                    ],
                    if (info.operationalCapabilities != null) ...[
                      _Section(
                        title: 'Distributed capability surface',
                        child: _OperationalCapabilitiesPanel(
                          key: const Key('operational-capabilities-panel'),
                          info: info.operationalCapabilities!,
                          api: _api,
                          showRecoveryRows: info.deployment == null,
                        ),
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                  if (info?.deployment == null &&
                      info?.operationalCapabilities == null)
                    const _OutputText(
                        'Single PC: no deployment profile reported.'),
                ],
              ),
              _group(
                'jobs',
                'Jobs, fanout & compute',
                children: [JobsPanel(source: _data, now: widget.now)],
              ),
              _group(
                'actions',
                'Server actions',
                children: [
                  if (!LocalManager.canRunLocalTools) ...[
                    const WorkspaceNotice(
                      message:
                          'This client cannot inspect local files or launch local processes. '
                          'Use the desktop app for local setup. Authenticated host-launcher controls remain available when configured in Settings.',
                    ),
                    const SizedBox(height: 12),
                  ],
                  if (localInfo != null && LocalManager.canRunLocalTools) ...[
                    _Section(
                      title: 'Install',
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          _StatusRow(
                            label: 'Platform',
                            value: localInfo.platform,
                            ok: true,
                          ),
                          _StatusRow(
                            label: 'Local system',
                            value: localInfo.systemExists
                                ? localInfo.systemDir
                                : 'Not bundled',
                            ok: localInfo.systemExists,
                          ),
                          _StatusRow(
                            label: 'Shared memory',
                            value: localInfo.sharedHome,
                            ok: true,
                            onCopy: () => _copy(localInfo.sharedHome),
                          ),
                          _StatusRow(
                            label: 'Local server',
                            value: LocalManager.canRunLocalTools
                                ? (localInfo.defaultServerReachable
                                    ? 'Reachable on 127.0.0.1:11435'
                                    : 'Not detected on 127.0.0.1:11435')
                                : widget.settings.serverUrl,
                            ok: LocalManager.canRunLocalTools
                                ? localInfo.defaultServerReachable
                                : _info != null,
                          ),
                          _StatusRow(
                            label: 'Updater',
                            value: localInfo.gitCheckout
                                ? 'Git pull enabled'
                                : 'First update will replace bundled folder from Git',
                            ok: true,
                          ),
                          _StatusRow(
                            label: 'Host runtime setup',
                            value: localInfo.bootstrapScript
                                ? 'One-click setup available'
                                : 'Bootstrap script not bundled',
                            ok: localInfo.bootstrapScript ||
                                !localInfo.canLaunch,
                          ),
                          _StatusRow(
                            label: 'Runtime payload',
                            value: localInfo.engineBundle
                                ? 'Sealed offline engine included'
                                : 'Host runtimes; downloads may be needed',
                            ok: localInfo.engineBundle,
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 12),
                  ],
                  _Section(
                    title: 'Host launcher',
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _StatusRow(
                          label: 'Control endpoint',
                          value: widget.settings.effectiveLauncherUrl.isEmpty
                              ? 'Not configured'
                              : widget.settings.effectiveLauncherUrl,
                          ok: widget.settings.usesHostLauncher &&
                              _launcherInfo != null,
                          off: !widget.settings.hasHostLauncher,
                        ),
                        _StatusRow(
                          label: 'Launcher',
                          value: _launcherInfo?.launcher ?? 'Not reachable',
                          ok: _launcherInfo?.ok ?? false,
                          off: !widget.settings.hasHostLauncher,
                        ),
                        _StatusRow(
                          label: 'Main server',
                          value: launcherServerText,
                          ok: _launcherInfo?.serverState == 'healthy',
                          off: !widget.settings.hasHostLauncher,
                        ),
                        if (_launcherOperation != null)
                          _StatusRow(
                            label: hostOperationActive
                                ? 'Active operation'
                                : 'Last operation',
                            value: _launcherOperation!.action.isEmpty
                                ? _launcherOperation!.phase
                                : '${_launcherOperation!.action}: '
                                    '${_launcherOperation!.phase}',
                            ok: _launcherOperation!.succeeded ||
                                hostOperationActive,
                          ),
                        if (_launcherError.isNotEmpty) ...[
                          const SizedBox(height: 8),
                          Text(
                            _launcherError,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error),
                          ),
                        ],
                        if (widget.settings.launcherConfigurationError !=
                            null) ...[
                          const SizedBox(height: 8),
                          Text(
                            widget.settings.launcherConfigurationError!,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error),
                          ),
                        ],
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                  _Section(
                    title: 'Runtime control',
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            FilledButton.icon(
                              onPressed: _working || !localRuntimeControls
                                  ? null
                                  : () => _run(
                                        () => LocalManager.setupEngine(
                                          allowHosted:
                                              widget.settings.allowHosted,
                                          contextSize:
                                              widget.settings.contextSize,
                                        ),
                                        label: 'Setup host runtime',
                                      ),
                              icon: const Icon(Icons.auto_fix_high_outlined),
                              label: const Text('Setup host runtime'),
                            ),
                            FilledButton.icon(
                              key: const Key('start-server'),
                              onPressed: _working ||
                                      hostOperationActive ||
                                      !canControlServer
                                  ? null
                                  : () =>
                                      _run(_startServer, label: 'Start server'),
                              icon: _busyIcon(
                                'Start server',
                                const Icon(Icons.play_arrow_outlined),
                              ),
                              label: Text(
                                _busyAction == 'Start server'
                                    ? 'Starting server...'
                                    : 'Start server',
                              ),
                            ),
                            FilledButton.tonalIcon(
                              onPressed: _working ||
                                      hostOperationActive ||
                                      !canControlServer
                                  ? null
                                  : () =>
                                      _run(_stopServer, label: 'Stop server'),
                              icon: _busyIcon(
                                'Stop server',
                                const Icon(Icons.stop_circle_outlined),
                              ),
                              label: const Text('Stop server'),
                            ),
                            FilledButton.tonalIcon(
                              onPressed: _working ||
                                      hostOperationActive ||
                                      !widget.settings.usesHostLauncher
                                  ? null
                                  : () => _run(
                                        _restartServer,
                                        label: 'Restart server',
                                      ),
                              icon: _busyIcon(
                                'Restart server',
                                const Icon(Icons.restart_alt),
                              ),
                              label: const Text('Restart server'),
                            ),
                            if (_waitingForLauncherOperation)
                              OutlinedButton.icon(
                                key: const Key('launcher-stop-waiting'),
                                onPressed: _stopWaitingForLauncherAction,
                                icon: const Icon(Icons.close),
                                label: const Text('Stop waiting'),
                              ),
                            FilledButton.tonalIcon(
                              onPressed: _working || !localRuntimeControls
                                  ? null
                                  : () => _run(
                                        LocalManager.startEndlessTraining,
                                        label: 'Grounded practice',
                                      ),
                              icon: const Icon(Icons.all_inclusive),
                              label: const Text('Grounded practice'),
                            ),
                            OutlinedButton.icon(
                              onPressed: _working || !localRuntimeControls
                                  ? null
                                  : () => _run(
                                        LocalManager.updateFromGit,
                                        label: 'Update from Git',
                                      ),
                              icon: const Icon(Icons.system_update_alt),
                              label: const Text('Update from Git'),
                            ),
                          ],
                        ),
                        if (_busyAction.isNotEmpty) ...[
                          const SizedBox(height: 12),
                          Row(
                            key: const Key('runtime-busy'),
                            children: [
                              const SizedBox(
                                width: 16,
                                height: 16,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2),
                              ),
                              const SizedBox(width: 10),
                              Expanded(
                                child: Text('$_busyAction in progress...'),
                              ),
                            ],
                          ),
                        ],
                        if (_runtimeFailure != null) ...[
                          const SizedBox(height: 12),
                          _RuntimeFailureCard(
                            label: _runtimeFailureLabel,
                            result: _runtimeFailure!,
                            onShowLog: () => unawaited(
                              _showActionFailure(
                                _runtimeFailureLabel,
                                _runtimeFailure!,
                              ),
                            ),
                          ),
                        ],
                        if (!localRuntimeControls) ...[
                          const SizedBox(height: 10),
                          Text(
                            widget.settings.usesHostLauncher
                                ? 'Start, Stop, and Restart control the configured host. '
                                    'Runtime setup, Git updates, grounded practice, and '
                                    'PEFT adapter training remain on that host.'
                                : 'Configure an explicit host launcher URL to control a server from this client-only device.',
                          ),
                        ],
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                  _Section(
                    title: 'Quick commands',
                    child: Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/stats'),
                          icon: const Icon(Icons.query_stats),
                          label: const Text('Stats'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/context'),
                          icon: const Icon(Icons.monitor_heart_outlined),
                          label: const Text('Context'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/compact'),
                          icon: const Icon(Icons.compress_outlined),
                          label: const Text('Compact'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/todo'),
                          icon: const Icon(Icons.task_alt_outlined),
                          label: const Text('Tasks'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/quality'),
                          icon: const Icon(Icons.fact_check_outlined),
                          label: const Text('Quality'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/improve'),
                          icon: const Icon(Icons.tips_and_updates_outlined),
                          label: const Text('Improve'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/agents'),
                          icon: const Icon(Icons.hub_outlined),
                          label: const Text('Agents'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/capacity'),
                          icon: const Icon(Icons.memory_outlined),
                          label: const Text('Capacity'),
                        ),
                        OutlinedButton.icon(
                          onPressed: _working ? null : _cancelActiveAgents,
                          icon: const Icon(Icons.cancel_schedule_send_outlined),
                          label: const Text('Cancel active'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/commands'),
                          icon: const Icon(Icons.terminal_outlined),
                          label: const Text('Commands'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/dump app'),
                          icon: const Icon(Icons.description_outlined),
                          label: const Text('Dump'),
                        ),
                        OutlinedButton.icon(
                          onPressed: _working
                              ? null
                              : () => _sendCommand('/permissions'),
                          icon: const Icon(Icons.security_outlined),
                          label: const Text('Permissions'),
                        ),
                        SizedBox(
                          width: 120,
                          child: TextField(
                            controller: _trainCount,
                            enabled: !_working,
                            keyboardType: TextInputType.number,
                            decoration: const InputDecoration(
                              isDense: true,
                              labelText: 'Practice cases',
                              border: OutlineInputBorder(),
                            ),
                          ),
                        ),
                        OutlinedButton.icon(
                          onPressed: _working
                              ? null
                              : () => _sendCommand(_trainCommand()),
                          icon: const Icon(Icons.school_outlined),
                          label: const Text('Run practice'),
                        ),
                        OutlinedButton.icon(
                          onPressed:
                              _working ? null : () => _sendCommand('/help'),
                          icon: const Icon(Icons.help_outline),
                          label: const Text('Help'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (_notice != null) ...[
                    WorkspaceNotice(
                        key: const Key('runtime-info-notice'),
                        message: _notice!),
                    const SizedBox(height: 12),
                  ],
                  _Section(
                    title: 'Command',
                    child: Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: _customCommand,
                            enabled: !_working,
                            autocorrect: false,
                            decoration: const InputDecoration(
                              isDense: true,
                              hintText: '/diagnostics',
                              border: OutlineInputBorder(),
                            ),
                            onSubmitted: (_) {
                              if (!_working) _sendCommand(_customCommand.text);
                            },
                          ),
                        ),
                        const SizedBox(width: 8),
                        FilledButton.icon(
                          onPressed: _working
                              ? null
                              : () => _sendCommand(_customCommand.text.trim()),
                          icon: const Icon(Icons.terminal),
                          label: const Text('Send'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (info != null) ...[
                    _Section(
                      title: 'Server state',
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (info.dbPath.isNotEmpty)
                            _StatusRow(
                              label: 'Database',
                              value: info.dbPath,
                              ok: true,
                              onCopy: () => _copy(info.dbPath),
                            ),
                          if (info.stateHome.isNotEmpty)
                            _StatusRow(
                              label: 'Home',
                              value: info.stateHome,
                              ok: true,
                              onCopy: () => _copy(info.stateHome),
                            ),
                          if (info.dbPath.isEmpty && info.stateHome.isEmpty)
                            const _OutputText(
                                'Server did not report state paths.'),
                        ],
                      ),
                    ),
                    const SizedBox(height: 12),
                    _Section(title: 'Status', child: _OutputText(info.status)),
                  ],
                  _Section(
                    title: 'Runtime architecture',
                    child: Text(
                      'Sonder Runtime is the orchestration layer, not a standalone '
                      'foundation model. Ollama loads and serves selected local '
                      'base-model weights for inference. Sonder Runtime supplies '
                      'routing, prompts, memory, tools, and policy. QLoRA/LoRA adapter '
                      'training runs through PEFT/Hugging Face; only validated '
                      'adapters or merged models are deployed to Ollama.',
                      style: Theme.of(context).textTheme.bodyMedium,
                    ),
                  ),
                  const SizedBox(height: 24),
                  Text(
                    'Sonder Runtime orchestrates inference; it is not the model itself. '
                    'Desktop builds look for a bundled local-system folder next to the app. '
                    'A sealed engine payload can include Python, Ollama, and models for offline setup; '
                    'otherwise setup uses installed runtimes and may download missing components. '
                    '${LocalManager.canRunLocalTools ? 'Runtime memory is shared through ${localInfo?.sharedHome ?? LocalManager.sharedHomePath()}. ' : 'This client cannot inspect the host memory directory. '}'
                    'Android, iOS, and other client-only builds use the authenticated '
                    'host launcher to start or stop the configured computer without '
                    'exposing a remote shell.',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ],
              ),
            ],
          );
          final column = Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints:
                  const BoxConstraints(maxWidth: conversationWidth + 32),
              child: content,
            ),
          );
          if (!wide) return column;
          return Row(
            children: [
              _SystemRail(destinations: destinations, onSelect: _jumpTo),
              const VerticalDivider(width: 1),
              Expanded(child: column),
            ],
          );
        },
      ),
    );
  }
}
