import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'agent_command_id.dart';
import 'agent_lanes.dart';
import 'agents/agent_status.dart';
import 'agents/background_detail.dart';
import 'agents/composer.dart';
import 'agents/list_pane.dart';
import 'agents/shortcuts.dart';
import 'agents/transcript_model.dart';
import 'agents/transcript_view.dart';
import 'api.dart';
import 'background_work.dart';
import 'theme.dart';
import 'ui/kit.dart';
import 'workspace_ui.dart';

/// Asks whether the person may leave Agents: resolves false when they chose
/// to keep their unsent drafts or unconfirmed requests.
typedef AgentLeaveGuard = Future<bool> Function();

/// Agent conversations (lanes) and background work (fleets, autopilot).
///
/// The server owns every status, message and report; this screen polls,
/// shows and sends commands, and keeps per-conversation drafts and scroll
/// positions for its lifetime (UX-CONTRACT.md).
class AgentScreen extends StatefulWidget {
  final SonderApi api;
  final String? initialLaneId;

  /// Project selected in Chat, retained for cancellation command context.
  final String initialProject;
  final ValueChanged<WorkspaceDestination>? onNavigate;

  /// Lets the app shell run this screen's leave guard before it switches
  /// destinations (sidebar, drawer, shortcuts). Called with the guard when
  /// the screen mounts and with null when it unmounts; store it without a
  /// rebuild. With a [ShellScope] and this hook, in-page links (Open
  /// Runtime, Go to Chat) leave through `ShellScope.navigate` and rely on
  /// the shell to ask; without the hook the screen asks first itself.
  final void Function(AgentLeaveGuard? guard)? registerLeaveGuard;

  const AgentScreen({
    super.key,
    required this.api,
    this.initialLaneId,
    this.initialProject = '',
    this.onNavigate,
    this.registerLeaveGuard,
  });

  @override
  State<AgentScreen> createState() => _AgentScreenState();
}

class _PendingCommand {
  final String id, action;
  final String? content;
  bool sending = false;
  String? error;
  _PendingCommand(this.action, this.content) : id = newAgentCommandId();
}

/// A control command the server did not confirm; its notice explains.
class _NotConfirmed implements Exception {
  const _NotConfirmed();
}

typedef _BackgroundRef = ({String kind, String id});

/// Width of the list pane beside the transcript: room for the five status
/// filters on one line where the page allows it.
double _listWidth(double pageWidth) => pageWidth >= 1100 ? 360 : 344;

/// Below this width the list and the transcript are separate pages.
const _splitBreakpoint = 850.0;

class _AgentScreenState extends State<AgentScreen> with WidgetsBindingObserver {
  final _lanes = <String, AgentLane>{};
  final _drafts = <String, TextEditingController>{};
  final _scrolls = <String, ScrollController>{};
  final _scrollOffsets = <String, double>{};
  final _snapshots = <String, AgentSnapshot>{};
  final _events = <String, Map<int, AgentEvent>>{};
  final _pending = <String, _PendingCommand>{};
  final _commandErrors = <String, ({String action, String message})>{};
  final _openTools = <String, Set<String>>{};
  final _openDetails = <String>{};
  final _reportOpen = <String, bool>{};
  BackgroundWork _background = const BackgroundWork();
  final _createdOrder = <String, int>{};
  String? _backgroundError;
  bool _backgroundPaused = false, _backgroundRefreshing = false;
  final _backgroundActionErrors = <String, String>{};
  final _reports = <String, AgentReport>{};
  final _reportParents = <String, String>{};
  final _reportCursors = <String, int>{};
  final _reportHasMore = <String, bool>{};
  final _reportErrors = <String, String>{};
  final _reportLoading = <String>{};
  String? _selected, _listError, _detailError;
  _BackgroundRef? _selectedBackground;
  RequestFailure? _listFailure, _detailFailure;
  bool _listPaused = false, _appActive = true, _visible = true;
  final _search = TextEditingController();
  final _searchFocus = FocusNode();

  /// One per conversation: two composers briefly coexist while the
  /// transcript pane cross-fades, and a focus node has one owner.
  final _composerFocus = <String, FocusNode>{};
  final _awayFromEnd = ValueNotifier<bool>(false);
  AgentFilter _filter = AgentFilter.all;
  bool _groupByParent = true;
  bool _loading = true, _refreshing = false, _hasMore = false;
  bool _manualRefreshing = false, _loadingMore = false;
  bool _initialConsumed = false;
  bool _wide = true;
  int _listCursor = 0, _generation = 0;
  int _loadedPages = 1;
  Timer? _timer;
  Timer? _watchTimer;
  Completer<void>? _watchDelay;

  bool get _active => _appActive && _visible;

  void _stopWatch() {
    _generation++;
    _watchTimer?.cancel();
    _watchDelay?.complete();
    _watchDelay = null;
  }

  Future<void> _delay(Duration duration) {
    final completer = Completer<void>();
    _watchDelay = completer;
    _watchTimer = Timer(duration, () {
      _watchDelay = null;
      completer.complete();
    });
    return completer.future;
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.registerLeaveGuard?.call(_confirmLeave);
    _loadLanes();
    _loadBackground();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) {
      _loadLanes();
      _loadBackground();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // A page kept alive offstage (covered by an opaque route, or a shell
    // that disables its tickers) stops inspecting, like a backgrounded app.
    final visible = TickerMode.valuesOf(context).enabled;
    if (visible == _visible) return;
    _visible = visible;
    if (!visible) {
      _stopWatch();
    } else {
      _resume();
    }
  }

  @override
  void dispose() {
    widget.registerLeaveGuard?.call(null);
    WidgetsBinding.instance.removeObserver(this);
    _stopWatch();
    _timer?.cancel();
    _search.dispose();
    _searchFocus.dispose();
    for (final node in _composerFocus.values) {
      node.dispose();
    }
    _awayFromEnd.dispose();
    for (final controller in _drafts.values) {
      controller.dispose();
    }
    for (final controller in [..._scrolls.values, ..._retiredScrolls]) {
      controller.dispose();
    }
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _appActive = state == AppLifecycleState.resumed;
    if (!_appActive) {
      _stopWatch();
    } else {
      _resume();
    }
  }

  void _resume() {
    if (!_active) return;
    unawaited(_loadLanes(manual: true));
    unawaited(_loadBackground(manual: true));
    if (_selected != null) _select(_selected!);
  }

  // -------------------------------------------------------------------------
  // Loading
  // -------------------------------------------------------------------------

  Future<void> _loadLanes({bool more = false, bool manual = false}) async {
    if (_refreshing || !_active || (_listPaused && !manual)) return;
    if (manual) _listPaused = false;
    _refreshing = true;
    try {
      var page = await widget.api.agentLanes(cursor: more ? _listCursor : 0);
      final lanes = [...page.lanes];
      if (!more) {
        for (var i = 1; i < _loadedPages && page.hasMore; i++) {
          page = await widget.api.agentLanes(cursor: page.nextCursor);
          lanes.addAll(page.lanes);
        }
      }
      if (!mounted) return;
      setState(() {
        for (final lane in lanes) {
          _mergeLane(lane);
        }
        if (more) _loadedPages++;
        _listCursor = page.nextCursor;
        _hasMore = page.hasMore;
        _listError = null;
        _listFailure = null;
      });
      final initial = widget.initialLaneId;
      if (!more && !_initialConsumed && initial != null) {
        if (_lanes.containsKey(initial)) {
          _initialConsumed = true;
          _select(initial);
        } else {
          try {
            final snapshot = await widget.api.agentInspect(initial);
            if (mounted && !_initialConsumed) {
              _mergeLane(snapshot.lane);
              _initialConsumed = true;
              _select(initial);
            }
          } catch (error) {
            if (mounted) {
              setState(() {
                _listFailure =
                    RequestFailure.read(error, resource: 'agent conversation');
                _listError = _listFailure!.message;
                _listPaused = true;
              });
            }
          }
        }
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _listFailure =
              RequestFailure.read(error, resource: 'agent conversations');
          _listError = _listFailure!.message;
          _listPaused = true;
        });
      }
    } finally {
      _refreshing = false;
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _loadBackground({bool manual = false}) async {
    if (!_active || _backgroundRefreshing || (_backgroundPaused && !manual)) {
      return;
    }
    _backgroundRefreshing = true;
    if (manual) _backgroundPaused = false;
    try {
      // Agents is a host-wide activity view: a selected Chat project scopes
      // new delegation/cancellation commands, but must not hide other work.
      final value = await widget.api.backgroundWork();
      if (mounted) {
        setState(() {
          _background = value;
          _backgroundError = null;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          final failure = RequestFailure.read(
            error,
            resource: 'background work',
          );
          _backgroundError = failure.message;
          if (failure.settingsRequired) _background = const BackgroundWork();
          _backgroundPaused = true;
        });
      }
    } finally {
      _backgroundRefreshing = false;
    }
  }

  Future<void> _refreshAll() async {
    setState(() => _manualRefreshing = true);
    try {
      await Future.wait([
        _loadLanes(manual: true),
        _loadBackground(manual: true),
      ]);
    } finally {
      if (mounted) setState(() => _manualRefreshing = false);
    }
  }

  Future<void> _loadMore() async {
    setState(() => _loadingMore = true);
    try {
      await _loadLanes(more: true, manual: true);
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  void _mergeLane(AgentLane lane) {
    if (lane.createdOrder > 0) _createdOrder[lane.id] = lane.createdOrder;
    if ((_lanes[lane.id]?.revision ?? -1) <= lane.revision) {
      _lanes[lane.id] = lane;
    }
  }

  // -------------------------------------------------------------------------
  // Selection and inspection
  // -------------------------------------------------------------------------

  /// Remembers where the open transcript was scrolled, so returning to it
  /// lands in the same place.
  void _rememberScroll() {
    final id = _selected;
    final scroll = id == null ? null : _scrolls[id];
    if (id != null && scroll != null && scroll.hasClients) {
      _scrollOffsets[id] = scroll.offset;
    }
  }

  ScrollController _scrollFor(String id) => _scrolls.putIfAbsent(
      id,
      () => ScrollController(
          initialScrollOffset: _scrollOffsets[id] ?? 0,
          keepScrollOffset: false));

  /// Controllers replaced while a fading view may still hold them. Each is
  /// disposed once nothing is attached, never while a view still scrolls.
  final _retiredScrolls = <ScrollController>[];

  void _retireScroll(String id) {
    final controller = _scrolls.remove(id);
    if (controller != null) _retiredScrolls.add(controller);
    _retiredScrolls.removeWhere((controller) {
      if (controller.hasClients) return false;
      controller.dispose();
      return true;
    });
  }

  void _select(String id) {
    if (_selected != id) {
      _rememberScroll();
      // A new view gets a new controller that starts at the remembered
      // offset; the outgoing view may still be fading out with the old one.
      _retireScroll(id);
      _awayFromEnd.value = false;
    }
    _stopWatch();
    setState(() {
      _selected = id;
      _selectedBackground = null;
      _detailError = null;
      _detailFailure = null;
    });
    final generation = ++_generation;
    unawaited(_watch(id, generation));
    unawaited(_loadReports(id));
  }

  void _selectBackground(String kind, String id) {
    _rememberScroll();
    _stopWatch();
    setState(() {
      _selected = null;
      _selectedBackground = (kind: kind, id: id);
    });
  }

  /// Narrow layouts: back from a transcript or detail to the list.
  void _closeDetail() {
    _rememberScroll();
    final id = _selected;
    if (id != null) _retireScroll(id);
    setState(() {
      _selected = null;
      _selectedBackground = null;
    });
    _stopWatch();
  }

  Future<void> _loadReports(String id, {bool more = false}) async {
    final lane = _lanes[id];
    if (lane == null || !_reportLoading.add(id)) return;
    try {
      // Reports are addressed to the parent session, which can belong to an
      // external harness. Show only reports authored by this selected lane.
      final page = await widget.api.agentReports(lane.parentSessionId,
          cursor: more ? (_reportCursors[id] ?? 0) : 0);
      if (!mounted) return;
      setState(() {
        for (final report in page.reports) {
          if (report.laneId != id) continue;
          if (_reports[report.id]?.acknowledged == true &&
              !report.acknowledged) {
            continue;
          }
          _reports[report.id] = report;
          _reportParents[report.id] = id;
        }
        if (more || !_reportCursors.containsKey(id)) {
          _reportCursors[id] = page.nextCursor;
          _reportHasMore[id] = page.hasMore;
        }
        _reportErrors.remove(id);
      });
    } catch (_) {
      if (mounted) {
        setState(
            () => _reportErrors[id] = 'Could not refresh reports to parent.');
      }
    } finally {
      _reportLoading.remove(id);
    }
  }

  Future<void> _watch(String id, int generation) async {
    var failures = 0;
    while (mounted && _active && generation == _generation) {
      try {
        final previous = _snapshots[id];
        final snapshot = await widget.api.agentInspect(
          id,
          cursor: previous?.nextCursor ?? 0,
          // Closing a browser request does not release a server-side wait.
          // Short reads keep rapid lane switches out of the shared wait cap.
          wait: false,
        );
        if (!mounted || generation != _generation) return;
        final grew = snapshot.events.isNotEmpty || previous == null;
        if (grew && previous != null) _followIfAtEnd(id);
        setState(() {
          _mergeLane(snapshot.lane);
          _snapshots[id] = snapshot;
          final history = _events.putIfAbsent(id, () => {});
          for (final event in snapshot.events) {
            history[event.sequence] = event;
          }
          _detailError = null;
          _detailFailure = null;
        });
        failures = 0;
        unawaited(_loadReports(id));
        if (!snapshot.hasMore) {
          await _delay(const Duration(seconds: 2));
        }
      } catch (error) {
        if (!mounted || generation != _generation) return;
        final failure =
            RequestFailure.read(error, resource: 'this conversation');
        failures++;
        final retry = failure.retryable && failures < 3;
        setState(() {
          _detailFailure = failure;
          _detailError =
              '${failure.message}${retry ? ' Reconnecting…' : failure.settingsRequired ? '' : ' Choose Retry to refresh.'}';
        });
        if (!retry) return;
        final delay = failure.retryAfterSeconds ?? (failures * 3);
        await _delay(Duration(seconds: delay.clamp(1, 60)));
      }
    }
  }

  /// Keeps a reader who is at the end of a transcript at the end as new
  /// events arrive; a reader scrolled up stays where they are.
  void _followIfAtEnd(String id) {
    final scroll = _scrolls[id];
    if (scroll == null || !scroll.hasClients) return;
    final position = scroll.position;
    if (position.maxScrollExtent - position.pixels > 96) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !scroll.hasClients) return;
      scroll.animateTo(scroll.position.maxScrollExtent,
          duration: SonderMotion.of(context, SonderMotion.medium),
          curve: SonderMotion.standard);
    });
  }

  void _goLatest(String id) {
    final scroll = _scrolls[id];
    if (scroll == null || !scroll.hasClients) return;
    scroll.animateTo(scroll.position.maxScrollExtent,
        duration: SonderMotion.of(context, SonderMotion.slow),
        curve: SonderMotion.standard);
  }

  // -------------------------------------------------------------------------
  // Leaving and navigation
  // -------------------------------------------------------------------------

  bool get _hasUnsentWork =>
      _pending.isNotEmpty ||
      _drafts.values.any((controller) => controller.text.trim().isNotEmpty);

  Future<bool> _confirmLeave() async {
    if (!_hasUnsentWork) return true;
    if (!mounted) return false;
    final leave = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
              title: const Text('Leave agent conversations?'),
              content: Text(_pending.isNotEmpty
                  ? 'A request has not been confirmed. Leaving closes its retry controls; the agent may still receive it. Unsent drafts will also be discarded.'
                  : 'Your unsent drafts will be discarded. Messages already sent remain in their conversations.'),
              actions: [
                TextButton(
                    autofocus: true,
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Keep editing')),
                TextButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('Leave conversations'))
              ],
            ));
    return leave == true;
  }

  bool get _canNavigate =>
      ShellScope.maybeOf(context) != null || widget.onNavigate != null;

  /// System back on a pushed Agents route: ask about drafts, then pop.
  /// `Navigator.pop` does not consult [PopScope], so it cannot ask twice.
  Future<void> _leaveRoute() async {
    if (!await _confirmLeave() || !mounted) return;
    final navigator = Navigator.of(context);
    if (navigator.canPop()) navigator.pop();
  }

  Future<void> _navigate(WorkspaceDestination destination) async {
    final shell = ShellScope.maybeOf(context);
    if (shell != null) {
      // The shell runs this page's ShellLeaveGuard (or a registered guard)
      // before it switches; asking here too would ask twice.
      final shellAsks =
          shell.leaveGuards != null || widget.registerLeaveGuard != null;
      if (!shellAsks && !await _confirmLeave()) return;
      if (mounted) shell.navigate(destination);
      return;
    }
    if (!await _confirmLeave() || !mounted) return;
    if (widget.onNavigate != null) {
      widget.onNavigate!(destination);
    } else if (destination == WorkspaceDestination.chat &&
        Navigator.of(context).canPop()) {
      Navigator.of(context).pop();
    }
  }

  // -------------------------------------------------------------------------
  // Commands
  // -------------------------------------------------------------------------

  void _sendSelected() {
    final lane = _lanes[_selected];
    final controller = _drafts[_selected];
    if (lane == null ||
        controller == null ||
        _detailFailure?.settingsRequired == true ||
        _pending.containsKey(lane.id) ||
        lane.status == 'cancelled' ||
        controller.text.trim().isEmpty ||
        (controller.value.composing.isValid &&
            !controller.value.composing.isCollapsed)) {
      return;
    }
    unawaited(_command(lane.id, 'messages', content: controller.text.trim()));
  }

  /// Sends (or retries, with the same command ID) a lane command. Returns
  /// whether the server confirmed it.
  Future<bool> _command(String id, String action, {String? content}) async {
    final pending = _pending.putIfAbsent(
      id,
      () => _PendingCommand(action, content),
    );
    if (pending.sending) return false;
    setState(() {
      pending.sending = true;
      pending.error = null;
      _commandErrors.remove(id);
    });
    try {
      final receipt = await widget.api.agentCommand(
        id,
        pending.action,
        commandId: pending.id,
        content: pending.content,
      );
      if (!mounted) return true;
      setState(() {
        if (receipt.lane != null) _mergeLane(receipt.lane!);
        if (pending.action == 'messages' &&
            _drafts[id]?.text.trim() == pending.content) {
          _drafts[id]?.clear();
        }
        _pending.remove(id);
      });
      if (_selected == id) _select(id);
      return true;
    } catch (error) {
      if (mounted) {
        setState(() {
          if (error is SonderException &&
              error.httpStatus != null &&
              error.httpStatus! >= 400 &&
              error.httpStatus! < 500 &&
              !error.retryable) {
            _pending.remove(id);
            _commandErrors[id] =
                (action: pending.action, message: error.message);
          } else {
            pending.error =
                'The server did not confirm this request. Retry checks the same request safely.';
            pending.sending = false;
          }
        });
      }
      return false;
    }
  }

  Future<void> _control(String id, String action) async {
    if (!await _command(id, action)) throw const _NotConfirmed();
  }

  Future<bool> _confirmCancel(AgentLane lane) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Cancel ${lane.displayTitle}?'),
        content: const Text(
          'Request cancellation of this agent’s work. The conversation remains available.',
        ),
        actions: [
          TextButton(
            autofocus: true,
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Keep working'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Cancel work'),
          ),
        ],
      ),
    );
    return confirmed == true && mounted;
  }

  Future<void> _ackReport(AgentReport report, String laneId) async {
    try {
      await widget.api.agentAcknowledge(
        report.id,
        commandId: 'read-${report.id}',
      );
    } catch (_) {
      if (mounted) {
        setState(
          () => _reportErrors[laneId] =
              'Could not confirm the report was marked read. Retry reports to check its current state.',
        );
      }
      rethrow;
    }
    await _loadReports(laneId);
    await _loadLanes(manual: true);
  }

  Future<bool> _confirmBackgroundCancel(String what, String title) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Cancel this $what?'),
        content: Text('Requests cancellation of “$title”. Its status updates '
            'here once the server confirms it.'),
        actions: [
          TextButton(
            autofocus: true,
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Keep running'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text('Cancel $what'),
          ),
        ],
      ),
    );
    return confirmed == true && mounted;
  }

  Future<void> _cancelBackground(String kind, String id, String title) async {
    if (!mounted) return;
    setState(() => _backgroundActionErrors.remove(id));
    try {
      await widget.api.cancelBackground(
        kind,
        id,
        project: widget.initialProject.trim(),
      );
    } catch (error) {
      if (mounted) {
        setState(() => _backgroundActionErrors[id] =
            RequestFailure.read(error, resource: 'cancellation').message);
      }
      rethrow;
    }
    if (!mounted) return;
    showSonderToast(context, 'Cancellation requested for “$title”');
    await _loadBackground(manual: true);
  }

  // -------------------------------------------------------------------------
  // List data
  // -------------------------------------------------------------------------

  List<AgentLane> _orderedLanes() {
    final ordered = _lanes.values.toList()
      ..sort((a, b) =>
          (_createdOrder[b.id] ?? 0).compareTo(_createdOrder[a.id] ?? 0));
    final result = <AgentLane>[];
    final seen = <String>{};
    void add(AgentLane lane) {
      if (!seen.add(lane.id)) return;
      result.add(lane);
      for (final child in ordered.where(
        (e) => e.parentLaneId == lane.id,
      )) {
        add(child);
      }
    }

    for (final lane in ordered.where(
      (e) => !_lanes.containsKey(e.parentLaneId),
    )) {
      add(lane);
    }
    for (final lane in ordered) {
      add(lane);
    }
    return result;
  }

  bool _matchesQuery(List<String> values) {
    final query = _search.text.trim().toLowerCase();
    return query.isEmpty ||
        values.any((value) => value.toLowerCase().contains(query));
  }

  bool _matches(AgentLane lane) {
    final parent = _lanes[lane.parentLaneId];
    return _matchesQuery([
          lane.displayTitle,
          lane.task,
          lane.workspaceRoot,
          parent?.displayTitle ?? '',
        ]) &&
        lane.matches(_filter);
  }

  bool _fleetMatches(BackgroundFleet fleet) =>
      backgroundMatches(fleet.status, _filter) &&
      _matchesQuery([
        fleet.task,
        fleet.id,
        for (final child in fleet.children) child.task,
      ]);

  bool _autopilotMatches(BackgroundAutopilot run) =>
      backgroundMatches(run.status, _filter) &&
      _matchesQuery([run.objective, run.id, run.currentTask]);

  Map<AgentFilter, int> _filterCounts() => {
        for (final filter in AgentFilter.values)
          filter: _lanes.values.where((lane) => lane.matches(filter)).length +
              _background.fleets
                  .where((f) => backgroundMatches(f.status, filter))
                  .length +
              _background.autopilot
                  .where((r) => backgroundMatches(r.status, filter))
                  .length,
      };

  String _rootParentId(AgentLane lane) {
    final seen = <String>{};
    while (seen.add(lane.id) && _lanes.containsKey(lane.parentLaneId)) {
      lane = _lanes[lane.parentLaneId]!;
    }
    return lane.parentSessionId;
  }

  /// Tree positions for rows shown together: depth counts only visible
  /// ancestors, so a filtered child never points at a missing parent.
  Map<String, TreePosition> _tree(List<AgentLane> rows) {
    final ids = {for (final lane in rows) lane.id};
    final parentOf = <String, String?>{};
    final children = <String, List<String>>{};
    for (final lane in rows) {
      final parent = lane.parentLaneId;
      final visible =
          parent != null && parent != lane.id && ids.contains(parent)
              ? parent
              : null;
      parentOf[lane.id] = visible;
      if (visible != null) children.putIfAbsent(visible, () => []).add(lane.id);
    }
    bool last(String id) {
      final parent = parentOf[id];
      return parent == null || children[parent]!.last == id;
    }

    final result = <String, TreePosition>{};
    for (final lane in rows) {
      final path = <String>[];
      final seen = <String>{lane.id};
      var parent = parentOf[lane.id];
      while (parent != null && seen.add(parent)) {
        path.insert(0, parent);
        parent = parentOf[parent];
      }
      final depth = path.length;
      result[lane.id] = TreePosition(
        depth: depth,
        rails: [for (var k = 0; k < depth - 1; k++) !last(path[k + 1])],
        last: last(lane.id),
        hasChildren: children[lane.id]?.isNotEmpty ?? false,
      );
    }
    return result;
  }

  String _shortId(String id) => id.length <= 12
      ? id
      : '${id.substring(0, 8)}…${id.substring(id.length - 4)}';

  Future<void> _showParent(String id) => showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
            title: const Text('Parent conversation'),
            content:
                SelectableText(id, style: SonderTokens.of(context).mono(13)),
            actions: [
              TextButton.icon(
                  onPressed: () async {
                    try {
                      await Clipboard.setData(ClipboardData(text: id));
                      if (context.mounted) {
                        showSonderToast(context, 'Parent ID copied');
                      }
                    } catch (_) {
                      if (context.mounted) {
                        showSonderToast(context,
                            'Could not copy. Select the ID above to copy it manually.',
                            kind: StatusKind.fail);
                      }
                    }
                  },
                  icon: const Icon(Icons.copy, size: 16),
                  label: const Text('Copy ID')),
              TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text('Close'))
            ],
          ));

  void _clearFilters() {
    setState(() {
      _search.clear();
      _filter = AgentFilter.all;
    });
    _searchFocus.requestFocus();
  }

  void _handleEscape() {
    if (_searchFocus.hasFocus && _search.text.isNotEmpty) {
      setState(() => _search.clear());
      return;
    }
    if (!_wide && (_selected != null || _selectedBackground != null)) {
      _closeDetail();
    }
  }

  void _moveSelection(int delta) {
    // Do not steal the normal word/cursor movement from a text field.
    if (_searchFocus.hasFocus ||
        _composerFocus.values.any((node) => node.hasFocus)) {
      return;
    }
    final lanes = _orderedLanes().where(_matches).toList();
    if (lanes.isEmpty) return;
    final current = _selected == null
        ? (delta > 0 ? -1 : lanes.length)
        : lanes.indexWhere((lane) => lane.id == _selected);
    final next = (current + delta).clamp(0, lanes.length - 1);
    _select(lanes[next].id);
  }

  // -------------------------------------------------------------------------
  // List pane
  // -------------------------------------------------------------------------

  Widget _searchAndFilters() {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.sm, SonderSpace.md, SonderSpace.sm, 0),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        TextField(
          key: const Key('agent-search'),
          controller: _search,
          focusNode: _searchFocus,
          style: Theme.of(context).textTheme.bodyMedium,
          decoration: InputDecoration(
            labelText: _hasMore
                ? 'Search loaded conversations'
                : 'Search conversations',
            floatingLabelBehavior: FloatingLabelBehavior.never,
            isDense: true,
            prefixIcon: Icon(Icons.search, size: 18, color: tokens.muted),
            prefixIconConstraints:
                const BoxConstraints(minWidth: 40, minHeight: 40),
            contentPadding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.sm, vertical: 13),
            suffixIcon: _search.text.isEmpty
                ? null
                : IconButton(
                    tooltip: 'Clear search',
                    iconSize: 16,
                    icon: const Icon(Icons.close),
                    onPressed: () {
                      setState(() => _search.clear());
                      _searchFocus.requestFocus();
                    }),
          ),
          onChanged: (_) => setState(() {}),
        ),
        const SizedBox(height: SonderSpace.xs),
        AgentFilterBar(
          value: _filter,
          counts: _filterCounts(),
          onChanged: (value) => setState(() => _filter = value),
        ),
      ]),
    );
  }

  Widget _refreshButton() => IconButton(
        tooltip: 'Refresh conversations',
        onPressed: _manualRefreshing ? null : _refreshAll,
        icon: _manualRefreshing
            ? const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Icon(Icons.refresh, size: 18),
      );

  Widget _notice(String title, {List<Widget> actions = const []}) => Padding(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.sm, SonderSpace.xs, SonderSpace.sm, SonderSpace.sm),
        child: WorkspaceNotice(
            kind: StatusKind.warn, title: title, actions: actions),
      );

  Widget _emptyState() => Center(
        key: const Key('agents-empty'),
        child: ConstrainedBox(
          // A comfortable measure for the sentence under the title.
          constraints: const BoxConstraints(maxWidth: 480),
          child: EmptyState(
            icon: Icons.account_tree_outlined,
            title: 'Agents start from Chat with /delegate',
            message: 'Ask Sonder to delegate a task, or type /delegate <task> '
                'in Chat. Each agent gets its own conversation here, and it '
                'stays available after the work finishes.',
            action: _canNavigate || Navigator.of(context).canPop()
                ? FilledButton.tonalIcon(
                    onPressed: () => _navigate(WorkspaceDestination.chat),
                    icon: const Icon(Icons.chat_bubble_outline, size: 18),
                    label: const Text('Go to Chat'))
                : null,
          ),
        ),
      );

  String _fleetSummary(BackgroundFleet fleet) {
    final p = fleetProgress(fleet);
    final s = backgroundStatus(fleet.status).kind;
    final when = s == StatusKind.running || s == StatusKind.note
        ? elapsedText(fleet.elapsedSeconds)
        : updatedAgo(fleet.updatedTs, _background.capturedAt) ??
            elapsedText(fleet.elapsedSeconds);
    return [
      '${p.done} of ${p.total} done',
      if (p.failed > 0) '${p.failed} failed',
      if (when != null) when,
    ].join(' · ');
  }

  String _autopilotSummary(BackgroundAutopilot run) {
    final total = run.taskCounts['total'] ?? 0;
    final s = backgroundStatus(run.status).kind;
    final when = s == StatusKind.running || s == StatusKind.note
        ? elapsedText(run.elapsedSeconds)
        : updatedAgo(run.updatedTs, _background.capturedAt) ??
            elapsedText(run.elapsedSeconds);
    return [
      if (total > 0) '${run.taskCounts['done'] ?? 0} of $total tasks',
      if (when != null) when,
    ].join(' · ');
  }

  List<Widget> _backgroundRows() {
    final fleets = _background.fleets.where(_fleetMatches).toList();
    final runs = _background.autopilot.where(_autopilotMatches).toList();
    if (fleets.isEmpty && runs.isEmpty && _backgroundError == null) {
      return const [];
    }
    final selected = _selectedBackground;
    return [
      AgentSectionHeader(
        key: const Key('background-group'),
        title: 'Background work',
        count: fleets.length + runs.length,
      ),
      if (_backgroundError != null)
        _notice(_backgroundError!, actions: [
          TextButton(
            onPressed: _backgroundRefreshing
                ? null
                : () => _loadBackground(manual: true),
            child: const Text('Retry background work'),
          ),
        ]),
      if (fleets.isNotEmpty)
        KeyedSubtree(
          key: const Key('fleet-group'),
          child: Column(children: [
            for (final fleet in fleets)
              BackgroundRow(
                key: ValueKey<String>('fleet-${fleet.id}'),
                kindLabel: 'Fleet',
                title: fleet.displayTask,
                status: fleet.status,
                detail: _fleetSummary(fleet),
                progress: fleetBar(context, fleet),
                selected: selected?.kind == 'fleet' && selected?.id == fleet.id,
                onTap: () => _selectBackground('fleet', fleet.id),
              ),
          ]),
        ),
      if (runs.isNotEmpty)
        KeyedSubtree(
          key: const Key('autopilot-group'),
          child: Column(children: [
            for (final run in runs)
              BackgroundRow(
                key: ValueKey<String>('autopilot-${run.id}'),
                kindLabel: 'Autopilot',
                title: run.displayObjective,
                status: run.status,
                detail: _autopilotSummary(run),
                progress: autopilotBar(context, run),
                selected:
                    selected?.kind == 'autopilot' && selected?.id == run.id,
                onTap: () => _selectBackground('autopilot', run.id),
              ),
          ]),
        ),
      if (_background.truncated)
        Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.xl, SonderSpace.xs, SonderSpace.md, 0),
          child: Text(
              'Some older runs or children are not loaded in this background snapshot.',
              style: Theme.of(context).textTheme.bodySmall),
        ),
      const SizedBox(height: SonderSpace.md),
    ];
  }

  List<Widget> _laneRows(List<AgentLane> ordered) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final empty = !_loading &&
        _lanes.isEmpty &&
        _background.isEmpty &&
        _backgroundError == null;
    final filtered = _filter != AgentFilter.all || _search.text.isNotEmpty;
    final rows = <Widget>[
      AgentSectionHeader(
        key: const Key('agent-lanes-group'),
        title: 'Conversations',
        count:
            _lanes.isEmpty ? null : (filtered ? ordered.length : _lanes.length),
        countSemantics: '${ordered.length} of ${_lanes.length} loaded'
            '${_hasMore ? ', more available' : ''}',
        actions: [
          if (_lanes.isNotEmpty)
            IconButton(
                tooltip:
                    _groupByParent ? 'Show a flat list' : 'Group by parent',
                isSelected: _groupByParent,
                onPressed: () =>
                    setState(() => _groupByParent = !_groupByParent),
                icon: const Icon(Icons.account_tree_outlined, size: 18)),
          _refreshButton(),
        ],
      ),
      if (_listError != null)
        _notice(_listError!, actions: [
          TextButton(
              onPressed: _listFailure?.settingsRequired == true && _canNavigate
                  ? () => _navigate(WorkspaceDestination.settings)
                  : () => _loadLanes(manual: true),
              child: Text(_listFailure?.settingsRequired == true && _canNavigate
                  ? 'Open Settings'
                  : 'Retry')),
        ]),
    ];
    if (empty) {
      rows.add(_emptyState());
      return rows;
    }
    if (_lanes.isEmpty) {
      if (!_loading) {
        rows.add(Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.xl, SonderSpace.xs, SonderSpace.md, SonderSpace.md),
          child: Text(
              'No agent conversations yet. Start one from Chat with /delegate.',
              style: text.bodySmall?.copyWith(color: tokens.muted)),
        ));
      }
      return rows;
    }
    if (ordered.isEmpty) {
      rows.add(Padding(
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.xl, vertical: SonderSpace.lg),
        child: Column(children: [
          Text('No loaded conversations match these filters.',
              textAlign: TextAlign.center,
              style: text.bodySmall?.copyWith(color: tokens.text2)),
          const SizedBox(height: SonderSpace.xs),
          TextButton(
              onPressed: _clearFilters, child: const Text('Clear filters')),
        ]),
      ));
    } else {
      final groups = <String, List<AgentLane>>{};
      for (final lane in ordered) {
        groups
            .putIfAbsent(_groupByParent ? _rootParentId(lane) : '', () => [])
            .add(lane);
      }
      for (final group in groups.entries) {
        final tree = _groupByParent ? _tree(group.value) : null;
        if (group.key.isNotEmpty) {
          rows.add(ParentGroupLabel(
            id: group.key,
            label: 'Parent · ${_shortId(group.key)}',
            onTap: () => _showParent(group.key),
          ));
        }
        for (final lane in group.value) {
          rows.add(LaneRow(
            key: ValueKey<String>('lane-${lane.id}'),
            lane: lane,
            selected: lane.id == _selected,
            tree: tree?[lane.id] ?? TreePosition.root,
            age: lane.updatedAt == null
                ? null
                : '${compactSpan(DateTime.now().toUtc().difference(lane.updatedAt!).abs())} ago',
            onTap: () => _select(lane.id),
          ));
        }
      }
    }
    if (_hasMore) {
      rows.add(Padding(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.md, SonderSpace.md, SonderSpace.md, 0),
        child: Column(children: [
          Text('${_lanes.length} loaded · more on the server',
              style: text.bodySmall?.copyWith(color: tokens.muted)),
          const SizedBox(height: SonderSpace.xs),
          OutlinedButton(
            onPressed: _loadingMore || _refreshing ? null : _loadMore,
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              if (_loadingMore) ...[
                const SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 2)),
                const SizedBox(width: SonderSpace.sm),
              ],
              const Flexible(
                child: Text('Load more conversations',
                    textAlign: TextAlign.center),
              ),
            ]),
          ),
        ]),
      ));
    }
    return rows;
  }

  Widget _listPane({required bool wide}) {
    final tokens = SonderTokens.of(context);
    final ordered = _orderedLanes().where(_matches).toList();
    final hasItems = _lanes.isNotEmpty || !_background.isEmpty;
    return Container(
      color: wide ? tokens.panel : null,
      child: Column(children: [
        if (hasItems) _searchAndFilters(),
        Expanded(
          child: _loading
              ? const Padding(
                  padding: EdgeInsets.only(top: SonderSpace.sm),
                  child: SkeletonRows(
                      rows: 6, semanticLabel: 'Loading agent conversations'),
                )
              : FocusTraversalGroup(
                  child: ListView(
                    key: const Key('agent-list'),
                    padding: const EdgeInsets.fromLTRB(SonderSpace.sm,
                        SonderSpace.xs, SonderSpace.sm, SonderSpace.xxl),
                    children: [
                      ..._backgroundRows(),
                      ..._laneRows(ordered),
                    ],
                  ),
                ),
        ),
      ]),
    );
  }

  // -------------------------------------------------------------------------
  // Transcript pane
  // -------------------------------------------------------------------------

  Widget _parentContext(AgentLane lane) {
    final tokens = SonderTokens.of(context);
    // Standard density: a compact button would shrink its 48 dp target.
    final style = TextButton.styleFrom(
      foregroundColor: tokens.text2,
      padding: const EdgeInsets.symmetric(horizontal: SonderSpace.sm),
    );
    final parent = _lanes[lane.parentLaneId];
    if (lane.parentLaneId != null) {
      return TextButton.icon(
          style: style,
          onPressed: () => _select(lane.parentLaneId!),
          icon: const Icon(Icons.arrow_upward, size: 14),
          label: Text(parent?.displayTitle ?? 'Parent conversation',
              overflow: TextOverflow.ellipsis));
    }
    if (lane.parentSessionId.isEmpty) return const SizedBox.shrink();
    return TextButton.icon(
        style: style,
        onPressed: () => _showParent(lane.parentSessionId),
        icon: const Icon(Icons.call_split, size: 14),
        label: Text('Parent conversation · ${_shortId(lane.parentSessionId)}',
            overflow: TextOverflow.ellipsis));
  }

  Widget _laneActions(AgentLane lane, _PendingCommand? pending) {
    final idle = pending == null;
    void quiet(Object error, StackTrace stack) {
      // The command notice beside the header explains the failure.
    }

    return Wrap(
      spacing: SonderSpace.sm,
      runSpacing: SonderSpace.sm,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        if (lane.canResume)
          AsyncActionButton(
            key: const Key('lane-resume'),
            label: 'Resume',
            icon: Icons.play_arrow,
            style: lane.needsAttention
                ? ActionButtonStyle.filled
                : ActionButtonStyle.outlined,
            doneLabel: null,
            onError: quiet,
            onPressed: idle ? () => _control(lane.id, 'resume') : null,
          ),
        if (lane.canInterrupt)
          AsyncActionButton(
            key: const Key('lane-interrupt'),
            label: 'Interrupt',
            icon: Icons.pause,
            doneLabel: null,
            onError: quiet,
            onPressed: idle ? () => _control(lane.id, 'interrupt') : null,
          ),
        if (lane.canCancel)
          AsyncActionButton(
            key: const Key('lane-cancel'),
            label: 'Cancel work',
            icon: Icons.stop_circle_outlined,
            style: ActionButtonStyle.text,
            doneLabel: null,
            onError: quiet,
            confirm: () => _confirmCancel(lane),
            onPressed: idle ? () => _control(lane.id, 'cancel') : null,
          ),
      ],
    );
  }

  Widget _laneHeader(AgentLane lane, _PendingCommand? pending, bool wide) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final pad = wide ? SonderSpace.xxl : SonderSpace.lg;
    final commandError = _commandErrors[lane.id];
    final controlPending = pending != null && pending.action != 'messages';
    final stopped =
        lane.needsAttention && lane.error.isNotEmpty ? lane.error : null;
    final facts = [
      lane.tier.isEmpty ? 'tier unavailable' : 'tier ${lane.tier}',
      'revision ${lane.revision}',
    ];
    final status = Semantics(
      container: true,
      label: 'Server execution status: ${lane.executionSummary}',
      liveRegion: true,
      excludeSemantics: true,
      child: SonderSwitcher(
        child: StatusPill(lane.statusKind,
            key: ValueKey(lane.status), word: lane.statusLabel),
      ),
    );
    final factsRow = Wrap(
      spacing: SonderSpace.sm,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        for (var i = 0; i < facts.length; i++) ...[
          if (i > 0)
            Text('·', style: tokens.mono(12, color: tokens.hairlineStrong)),
          Text(facts[i], style: tokens.mono(12, color: tokens.muted)),
        ],
      ],
    );
    final actions = _laneActions(lane, pending);
    return Padding(
      padding: EdgeInsets.fromLTRB(
          pad, wide ? SonderSpace.lg : SonderSpace.md, pad, SonderSpace.md),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: conversationWidth),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // The link's icon lines up with the title's first letter.
              Transform.translate(
                offset: const Offset(-SonderSpace.sm, 0),
                child: _parentContext(lane),
              ),
              Tooltip(
                message: lane.displayTitle,
                child: Text(lane.displayTitle,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: wide
                        ? text.titleLarge
                        : text.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600)),
              ),
              const SizedBox(height: SonderSpace.sm),
              LayoutBuilder(builder: (context, constraints) {
                final identity = Wrap(
                  spacing: SonderSpace.md,
                  runSpacing: SonderSpace.xs,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [status, factsRow],
                );
                if (constraints.maxWidth < 560) {
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      identity,
                      const SizedBox(height: SonderSpace.md),
                      actions,
                    ],
                  );
                }
                return Row(children: [
                  Expanded(child: identity),
                  const SizedBox(width: SonderSpace.md),
                  actions,
                ]);
              }),
              SonderReveal(
                visible: stopped != null,
                child: stopped == null
                    ? const SizedBox.shrink()
                    : Padding(
                        padding: const EdgeInsets.only(top: SonderSpace.md),
                        child: WorkspaceNotice(
                          kind: lane.status == 'failed'
                              ? StatusKind.fail
                              : StatusKind.warn,
                          word: lane.status == 'failed' ? null : 'needs you',
                          title: laneErrorSummary(stopped),
                          hint: laneErrorSummary(stopped) == stopped
                              ? null
                              : 'server code $stopped',
                          liveRegion: false,
                        ),
                      ),
              ),
              SonderReveal(
                visible: _detailError != null,
                child: _detailError == null
                    ? const SizedBox.shrink()
                    : Padding(
                        padding: const EdgeInsets.only(top: SonderSpace.md),
                        child: WorkspaceNotice(
                          kind: StatusKind.warn,
                          title: _detailError!,
                          actions: [
                            TextButton(
                                onPressed: _detailFailure?.settingsRequired ==
                                            true &&
                                        _canNavigate
                                    ? () =>
                                        _navigate(WorkspaceDestination.settings)
                                    : () => _select(lane.id),
                                child: Text(
                                    _detailFailure?.settingsRequired == true &&
                                            _canNavigate
                                        ? 'Open Settings'
                                        : 'Retry')),
                          ],
                        ),
                      ),
              ),
              SonderReveal(
                visible: controlPending ||
                    (commandError != null && commandError.action != 'messages'),
                child: Padding(
                  padding: const EdgeInsets.only(top: SonderSpace.md),
                  child: controlPending
                      ? _pendingNotice(lane, pending)
                      : commandError != null &&
                              commandError.action != 'messages'
                          ? WorkspaceNotice(
                              kind: StatusKind.fail,
                              title: commandError.message)
                          : const SizedBox.shrink(),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _pendingNotice(AgentLane lane, _PendingCommand pending) =>
      WorkspaceNotice(
        kind: pending.error == null ? StatusKind.running : StatusKind.warn,
        word: pending.error == null ? 'sending' : null,
        title: pending.error ??
            switch (pending.action) {
              'interrupt' => 'Sending the interrupt request…',
              'resume' => 'Sending the resume request…',
              'cancel' => 'Sending the cancel request…',
              _ => 'Sending your message…',
            },
        actions: [
          if (pending.error != null)
            TextButton(
                onPressed: () => _command(lane.id, pending.action),
                child: const Text('Retry request')),
        ],
      );

  Widget _transcriptItem(AgentLane lane, TranscriptItem item, bool needsResume,
      {int? explainedFailure}) {
    switch (item) {
      case MessageItem():
        return AgentTurnView(
            key: ValueKey(item.key), item: item, needsResume: needsResume);
      case ToolItem():
        final open = _openTools[lane.id]?.contains(item.callKey) ?? false;
        return ToolCallCard(
          key: ValueKey('tool-${lane.id}-${item.callKey}'),
          item: item,
          expanded: open,
          onToggle: () => setState(() {
            final set = _openTools.putIfAbsent(lane.id, () => {});
            if (!set.remove(item.callKey)) set.add(item.callKey);
          }),
        );
      case LifecycleItem():
        return LifecycleLine(
            key: ValueKey(item.key),
            item: item,
            showReason: item.sequence != explainedFailure);
    }
  }

  List<Widget> _reportSection(AgentLane lane) {
    final text = Theme.of(context).textTheme;
    final reports = _reports.values
        .where((report) => _reportParents[report.id] == lane.id)
        .toList();
    if (reports.isEmpty && _reportErrors[lane.id] == null) return const [];
    final unread = reports.where((r) => !r.acknowledged).length;
    return [
      const SizedBox(height: SonderSpace.xxl),
      Row(children: [
        Expanded(
          child: Semantics(
            header: true,
            child: Text('Reports to parent',
                style: text.titleSmall?.copyWith(fontWeight: FontWeight.w600)),
          ),
        ),
        if (unread > 0)
          CountBadge(unread,
              semantic: '$unread unread report${unread == 1 ? '' : 's'}'),
      ]),
      const SizedBox(height: SonderSpace.xs),
      Text('Marking a report read does not approve or integrate its changes.',
          style: text.bodySmall),
      const SizedBox(height: SonderSpace.md),
      if (_reportErrors[lane.id] != null)
        Padding(
          padding: const EdgeInsets.only(bottom: SonderSpace.md),
          child: WorkspaceNotice(
              kind: StatusKind.warn,
              title: _reportErrors[lane.id]!,
              actions: [
                TextButton(
                    onPressed: () => _loadReports(lane.id),
                    child: const Text('Retry reports')),
              ]),
        ),
      for (final report in reports)
        Padding(
          padding: const EdgeInsets.only(bottom: SonderSpace.md),
          child: ReportCard(
            key: ValueKey('report-${report.id}'),
            report: report,
            expanded: _reportOpen[report.id] ?? !report.acknowledged,
            onToggle: () => setState(() => _reportOpen[report.id] =
                !(_reportOpen[report.id] ?? !report.acknowledged)),
            onMarkRead:
                report.acknowledged ? null : () => _ackReport(report, lane.id),
            onMarkReadError: (_, __) {},
          ),
        ),
      if (_reportHasMore[lane.id] == true)
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton(
              onPressed: () => _loadReports(lane.id, more: true),
              child: const Text('Load more reports')),
        ),
    ];
  }

  Widget _transcript(AgentLane lane, {required bool wide}) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final snapshot = _snapshots[lane.id];
    final pending = _pending[lane.id];
    final controller = _drafts.putIfAbsent(lane.id, TextEditingController.new);
    final scroll = _scrollFor(lane.id);
    final needsResume = lane.needsAttention;
    final pad = wide ? SonderSpace.xxl : SonderSpace.lg;
    final canSend = pending == null &&
        lane.status != 'cancelled' &&
        _detailFailure?.settingsRequired != true &&
        controller.text.trim().isNotEmpty;
    final commandError = _commandErrors[lane.id];

    Widget body;
    if (snapshot == null) {
      body = ListView(
        padding: EdgeInsets.fromLTRB(pad, SonderSpace.lg, pad, SonderSpace.lg),
        children: [
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: conversationWidth),
              child: _detailError == null
                  ? const TranscriptSkeleton()
                  : Padding(
                      padding:
                          const EdgeInsets.symmetric(vertical: SonderSpace.xl),
                      child: Text('Conversation not loaded.',
                          style:
                              text.bodyMedium?.copyWith(color: tokens.text2)),
                    ),
            ),
          ),
        ],
      );
    } else {
      final items = buildTranscript(
        _events[lane.id]?.values ?? const <AgentEvent>[],
        snapshot.messages,
        active: lane.isWorking,
      );
      // The header notice explains the current failure; its marker in the
      // timeline then shows only what happened, not the reason again.
      int? explainedFailure;
      if (lane.needsAttention && lane.error.isNotEmpty) {
        for (final item in items.reversed) {
          if (item is LifecycleItem &&
              (item.kind == LifecycleKind.failed ||
                  item.kind == LifecycleKind.stalled)) {
            explainedFailure = item.sequence;
            break;
          }
        }
      }
      final hasDetails = lane.task.isNotEmpty ||
          lane.workspaceRoot.isNotEmpty ||
          lane.tier.isNotEmpty;
      final head = <Widget>[
        if (hasDetails)
          RunDetails(
            key: ValueKey('task-${lane.id}'),
            lane: lane,
            expanded: _openDetails.contains(lane.id),
            onToggle: () => setState(() {
              if (!_openDetails.remove(lane.id)) _openDetails.add(lane.id);
            }),
            onOpenRuntime: _canNavigate
                ? () => _navigate(WorkspaceDestination.runtime)
                : null,
          ),
        if (snapshot.hasMore)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: SonderSpace.md),
            child: Text('Loading conversation history…',
                style: text.bodySmall?.copyWith(color: tokens.muted)),
          ),
        if (items.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: SonderSpace.xl),
            child: Text('This conversation has no messages yet.',
                style: text.bodyMedium?.copyWith(color: tokens.text2)),
          ),
      ];
      final tail = _reportSection(lane);
      final count = head.length + items.length + tail.length;
      // Every item spans the reading column, so short text starts at its
      // left edge instead of being centred.
      Widget readable(Widget child) => Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: conversationWidth),
              child: SizedBox(width: double.infinity, child: child),
            ),
          );
      final list = ListView.builder(
        key: const Key('agent-transcript'),
        // The controller is retained per lane in _scrolls; per-item state
        // (open tool cards, reports, details) lives in this screen, so items
        // can be rebuilt lazily without losing it.
        controller: scroll,
        padding: EdgeInsets.fromLTRB(pad, SonderSpace.md, pad, SonderSpace.xxl),
        itemCount: count,
        itemBuilder: (context, index) {
          if (index < head.length) return readable(head[index]);
          index -= head.length;
          if (index < items.length) {
            return readable(_transcriptItem(lane, items[index], needsResume,
                explainedFailure: explainedFailure));
          }
          return readable(tail[index - items.length]);
        },
      );
      bool track(ScrollMetrics metrics, int depth) {
        if (depth != 0) return false;
        final away = metrics.maxScrollExtent - metrics.pixels > 240;
        if (_awayFromEnd.value != away) _awayFromEnd.value = away;
        return false;
      }

      body = Stack(children: [
        NotificationListener<ScrollMetricsNotification>(
          onNotification: (n) => track(n.metrics, n.depth),
          child: NotificationListener<ScrollUpdateNotification>(
            onNotification: (n) => track(n.metrics, n.depth),
            child: list,
          ),
        ),
        Positioned(
          left: 0,
          right: 0,
          bottom: SonderSpace.md,
          child: Center(
            child: ValueListenableBuilder<bool>(
              valueListenable: _awayFromEnd,
              builder: (context, away, _) => SonderSwitcher(
                alignment: Alignment.bottomCenter,
                child: away
                    ? _LatestButton(
                        key: const ValueKey('latest'),
                        onTap: () => _goLatest(lane.id))
                    : const SizedBox.shrink(key: ValueKey('none')),
              ),
            ),
          ),
        ),
      ]);
    }

    final messagePending = pending != null && pending.action == 'messages';
    final messageError =
        commandError != null && commandError.action == 'messages'
            ? commandError.message
            : null;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _laneHeader(lane, pending, wide),
      Divider(height: 1, color: tokens.hairline),
      Expanded(child: body),
      SonderReveal(
        visible: messagePending || messageError != null,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.md, SonderSpace.sm, SonderSpace.md, 0),
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: conversationWidth),
              child: messagePending
                  ? _pendingNotice(lane, pending)
                  : messageError != null
                      ? WorkspaceNotice(
                          kind: StatusKind.fail, title: messageError)
                      : const SizedBox.shrink(),
            ),
          ),
        ),
      ),
      AgentComposer(
        controller: controller,
        focusNode: _composerFocus.putIfAbsent(lane.id, FocusNode.new),
        fieldKey: ValueKey('composer-${lane.id}'),
        enabled: pending == null &&
            lane.status != 'cancelled' &&
            _detailFailure?.settingsRequired != true,
        canSend: canSend,
        onSend: _sendSelected,
        onChanged: (_) => setState(() {}),
        note: lane.status == 'cancelled'
            ? 'This conversation was cancelled. Its messages remain available.'
            : needsResume
                ? 'Messages wait here until you choose Resume.'
                : null,
        noteKind: needsResume ? StatusKind.warn : StatusKind.note,
        showHint: MediaQuery.sizeOf(context).width >= 600,
      ),
    ]);
  }

  /// Identity of what the detail pane shows, for its cross-fade.
  String get _detailKey {
    final background = _selectedBackground;
    if (_selected != null) return 'lane-$_selected';
    if (background != null) return '${background.kind}-${background.id}';
    return 'none';
  }

  /// Whether the selected fleet or autopilot run is still in the snapshot.
  bool get _backgroundSelectionExists {
    final ref = _selectedBackground;
    if (ref == null) return false;
    return ref.kind == 'fleet'
        ? _background.fleets.any((f) => f.id == ref.id)
        : _background.autopilot.any((r) => r.id == ref.id);
  }

  Widget? _backgroundDetail({required bool wide}) {
    final ref = _selectedBackground;
    if (ref == null) return null;
    if (ref.kind == 'fleet') {
      final fleet = _background.fleets.where((f) => f.id == ref.id).firstOrNull;
      if (fleet == null) return null;
      return FleetDetailView(
        fleet: fleet,
        capturedAt: _background.capturedAt,
        narrow: !wide,
        error: _backgroundActionErrors[fleet.id],
        confirmCancel: () =>
            _confirmBackgroundCancel('fleet', fleet.displayTask),
        onCancel: () => _cancelBackground('fleet', fleet.id, fleet.displayTask),
        onCancelError: (_, __) {},
        onCancelChild: (child) async {
          if (!await _confirmBackgroundCancel('agent', child.displayTask)) {
            return;
          }
          try {
            await _cancelBackground('fleet', child.id, child.displayTask);
          } catch (_) {
            if (mounted) {
              showSonderToast(
                  context,
                  _backgroundActionErrors[child.id] ??
                      'Could not request cancellation.',
                  kind: StatusKind.fail);
            }
          }
        },
        hasLane: _lanes.containsKey,
        onOpenLane: _select,
      );
    }
    final run = _background.autopilot.where((r) => r.id == ref.id).firstOrNull;
    if (run == null) return null;
    return AutopilotDetailView(
      run: run,
      capturedAt: _background.capturedAt,
      narrow: !wide,
      error: _backgroundActionErrors[run.id],
      confirmCancel: () =>
          _confirmBackgroundCancel('autopilot run', run.displayObjective),
      onCancel: () =>
          _cancelBackground('autopilot', run.id, run.displayObjective),
      onCancelError: (_, __) {},
    );
  }

  Widget _detailPane({required bool wide}) {
    final lane = _lanes[_selected];
    if (lane != null) {
      return KeyedSubtree(
          key: ValueKey('lane-pane-${lane.id}'),
          child: _transcript(lane, wide: wide));
    }
    final background = _backgroundDetail(wide: wide);
    if (background != null) return background;
    if (_loading) return const SizedBox.shrink();
    return const Align(
      alignment: Alignment(0, -0.2),
      child: EmptyState(
        icon: Icons.forum_outlined,
        title: 'Select an agent conversation',
        message: 'Its transcript, tool calls and reports open here.',
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Page
  // -------------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final shell = ShellScope.maybeOf(context);
    final tokens = SonderTokens.of(context);
    // Unsent drafts and uncertain commands stop the shell's sidebar, drawer,
    // shortcuts and system back, as they stop in-page navigation.
    return ShellLeaveGuard(
        canLeave: _confirmLeave,
        child: LayoutBuilder(
          builder: (context, constraints) {
            final wide = constraints.maxWidth >= _splitBreakpoint;
            _wide = wide;
            final hasDetail =
                _lanes[_selected] != null || _backgroundSelectionExists;
            final narrowDetail = !wide && hasDetail;
            final empty = !_loading &&
                _lanes.isEmpty &&
                _background.isEmpty &&
                _backgroundError == null;
            void focusSearch() {
              if (narrowDetail) _closeDetail();
              WidgetsBinding.instance.addPostFrameCallback((_) {
                if (mounted) _searchFocus.requestFocus();
              });
            }

            final Widget? leading = narrowDetail
                ? IconButton(
                    tooltip: 'All agent conversations',
                    icon: const Icon(Icons.arrow_back),
                    onPressed: _closeDetail,
                  )
                : shell != null
                    ? (shell.sidebarVisible
                        ? null
                        : IconButton(
                            tooltip: 'Open navigation',
                            icon: const Icon(Icons.menu),
                            onPressed: shell.openNavigation,
                          ))
                    : Navigator.of(context).canPop()
                        ? IconButton(
                            tooltip: 'Back to chat',
                            icon: const Icon(Icons.arrow_back),
                            onPressed: () =>
                                _navigate(WorkspaceDestination.chat))
                        : null;

            final Widget body;
            if (wide && empty) {
              body = Align(
                alignment: Alignment.topCenter,
                child: ConstrainedBox(
                  constraints:
                      const BoxConstraints(maxWidth: conversationWidth),
                  child: _listPane(wide: false),
                ),
              );
            } else if (wide) {
              body = Row(children: [
                SizedBox(
                    width: _listWidth(constraints.maxWidth),
                    child: _listPane(wide: true)),
                VerticalDivider(width: 1, thickness: 1, color: tokens.hairline),
                Expanded(
                  child: FocusTraversalGroup(
                    child: SonderSwitcher(
                      child: KeyedSubtree(
                        key: ValueKey(_detailKey),
                        child: _detailPane(wide: true),
                      ),
                    ),
                  ),
                ),
              ]);
            } else {
              body = SonderSwitcher(
                child: KeyedSubtree(
                  key: ValueKey(narrowDetail ? _detailKey : 'list'),
                  child: narrowDetail
                      ? _detailPane(wide: false)
                      : _listPane(wide: false),
                ),
              );
            }

            return PopScope(
              canPop: !narrowDetail && (shell != null || !_hasUnsentWork),
              onPopInvokedWithResult: (didPop, _) async {
                if (didPop) return;
                if (narrowDetail) {
                  _closeDetail();
                  return;
                }
                await _leaveRoute();
              },
              child: CallbackShortcuts(
                bindings: {
                  const SingleActivator(LogicalKeyboardKey.keyF,
                      control: true, shift: true): focusSearch,
                  const SingleActivator(LogicalKeyboardKey.keyF,
                      meta: true, shift: true): focusSearch,
                  const SingleActivator(LogicalKeyboardKey.arrowUp, alt: true):
                      () => _moveSelection(-1),
                  const SingleActivator(LogicalKeyboardKey.arrowDown,
                      alt: true): () => _moveSelection(1),
                  const SingleActivator(LogicalKeyboardKey.escape):
                      _handleEscape,
                },
                child: Focus(
                  autofocus: true,
                  child: Scaffold(
                    appBar: AppBar(
                      automaticallyImplyLeading: false,
                      leading: leading,
                      title: const Text('Agents'),
                      actions: [
                        // Wide layouts already show the list's search field;
                        // one search control per screen.
                        if (!wide &&
                            (_lanes.isNotEmpty || !_background.isEmpty))
                          IconButton(
                              tooltip: 'Find conversation (Ctrl+Shift+F)',
                              onPressed: focusSearch,
                              icon: const Icon(Icons.search)),
                        IconButton(
                            tooltip: 'Agent conversation shortcuts',
                            onPressed: () => showAgentShortcuts(context),
                            icon: const Icon(Icons.help_outline)),
                        if (shell == null && widget.onNavigate != null) ...[
                          WorkspaceMenu(
                              current: WorkspaceDestination.agents,
                              onSelected: _navigate),
                          TextButton.icon(
                              onPressed: () =>
                                  _navigate(WorkspaceDestination.chat),
                              icon: const Icon(Icons.chat_bubble_outline,
                                  size: 18),
                              label: const Text('Chat')),
                        ],
                        const SizedBox(width: SonderSpace.xs),
                      ],
                    ),
                    body: body,
                  ),
                ),
              ),
            );
          },
        ));
  }
}

/// "Latest": appears when the reader has scrolled away from the end.
class _LatestButton extends StatelessWidget {
  final VoidCallback onTap;
  const _LatestButton({super.key, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Tooltip(
      message: 'Go to latest activity',
      child: Material(
        color: tokens.raised,
        shape: StadiumBorder(side: BorderSide(color: tokens.hairlineStrong)),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 48),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: SonderSpace.lg),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                Icon(Icons.arrow_downward, size: 16, color: tokens.text2),
                const SizedBox(width: SonderSpace.sm),
                Text('Latest',
                    style: text.labelLarge?.copyWith(color: tokens.text)),
              ]),
            ),
          ),
        ),
      ),
    );
  }
}
