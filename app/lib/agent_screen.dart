import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'agent_lanes.dart';
import 'agent_command_id.dart';
import 'api.dart';
import 'theme.dart';
import 'workspace_ui.dart';

class AgentScreen extends StatefulWidget {
  final SonderApi api;
  final ValueChanged<WorkspaceDestination>? onNavigate;
  const AgentScreen({super.key, required this.api, this.onNavigate});
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

enum _AgentFilter { all, working, attention, unread, finished }

class _AgentScreenState extends State<AgentScreen> with WidgetsBindingObserver {
  final _lanes = <String, AgentLane>{};
  final _drafts = <String, TextEditingController>{};
  final _scrolls = <String, ScrollController>{};
  final _acknowledging = <String>{};
  final _snapshots = <String, AgentSnapshot>{};
  final _events = <String, Map<int, AgentEvent>>{};
  final _pending = <String, _PendingCommand>{};
  final _commandErrors = <String, String>{};
  final _reports = <String, AgentReport>{};
  final _reportParents = <String, String>{};
  final _reportCursors = <String, int>{};
  final _reportHasMore = <String, bool>{};
  final _reportErrors = <String, String>{};
  final _reportLoading = <String>{};
  String? _selected, _listError, _detailError;
  RequestFailure? _listFailure, _detailFailure;
  bool _listPaused = false, _appActive = true;
  final _search = TextEditingController();
  final _searchFocus = FocusNode();
  final _composerFocus = FocusNode();
  _AgentFilter _filter = _AgentFilter.all;
  bool _groupByParent = true;
  bool _loading = true, _refreshing = false, _hasMore = false;
  int _listCursor = 0, _generation = 0;
  int _loadedPages = 1;
  Timer? _timer;
  Timer? _watchTimer;
  Completer<void>? _watchDelay;

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
    _loadLanes();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _loadLanes());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stopWatch();
    _timer?.cancel();
    _search.dispose();
    _searchFocus.dispose();
    _composerFocus.dispose();
    for (final controller in _drafts.values) {
      controller.dispose();
    }
    for (final controller in _scrolls.values) {
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
      unawaited(_loadLanes(manual: true));
      if (_selected != null) _select(_selected!);
    }
  }

  Future<void> _loadLanes({bool more = false, bool manual = false}) async {
    if (_refreshing || !_appActive || (_listPaused && !manual)) return;
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

  void _mergeLane(AgentLane lane) {
    if ((_lanes[lane.id]?.revision ?? -1) <= lane.revision) {
      _lanes[lane.id] = lane;
    }
  }

  void _select(String id) {
    _stopWatch();
    setState(() {
      _selected = id;
      _detailError = null;
      _detailFailure = null;
    });
    final generation = ++_generation;
    unawaited(_watch(id, generation));
    unawaited(_loadReports(id));
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
    while (mounted && _appActive && generation == _generation) {
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

  Future<void> _navigate(WorkspaceDestination destination) async {
    final hasDraft =
        _drafts.values.any((controller) => controller.text.trim().isNotEmpty);
    if (hasDraft || _pending.isNotEmpty) {
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
      if (leave != true || !mounted) return;
    }
    if (widget.onNavigate != null) {
      widget.onNavigate!(destination);
    } else if (destination == WorkspaceDestination.chat && mounted) {
      Navigator.of(context).maybePop();
    }
  }

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

  Future<void> _command(String id, String action, {String? content}) async {
    final pending = _pending.putIfAbsent(
      id,
      () => _PendingCommand(action, content),
    );
    if (pending.sending) return;
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
      if (!mounted) return;
      setState(() {
        if (receipt.lane != null) _mergeLane(receipt.lane!);
        if (pending.action == 'messages' &&
            _drafts[id]?.text.trim() == pending.content) {
          _drafts[id]?.clear();
        }
        _pending.remove(id);
      });
      if (_selected == id) _select(id);
    } catch (error) {
      if (mounted) {
        setState(() {
          if (error is SonderException &&
              error.httpStatus != null &&
              error.httpStatus! >= 400 &&
              error.httpStatus! < 500 &&
              !error.retryable) {
            _pending.remove(id);
            _commandErrors[id] = error.message;
          } else {
            pending.error =
                'The server did not confirm this request. Retry checks the same request safely.';
            pending.sending = false;
          }
        });
      }
    }
  }

  Future<void> _cancel(AgentLane lane) async {
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
    if (confirmed == true && mounted) await _command(lane.id, 'cancel');
  }

  List<AgentLane> _orderedLanes() {
    final result = <AgentLane>[];
    final seen = <String>{};
    void add(AgentLane lane) {
      if (!seen.add(lane.id)) return;
      result.add(lane);
      for (final child in _lanes.values.where(
        (e) => e.parentLaneId == lane.id,
      )) {
        add(child);
      }
    }

    for (final lane in _lanes.values.where(
      (e) => !_lanes.containsKey(e.parentLaneId),
    )) {
      add(lane);
    }
    for (final lane in _lanes.values) {
      add(lane);
    }
    return result;
  }

  static const _filterLabels = {
    _AgentFilter.all: 'All statuses',
    _AgentFilter.working: 'Working',
    _AgentFilter.attention: 'Needs attention',
    _AgentFilter.unread: 'Unread reports',
    _AgentFilter.finished: 'Finished',
  };

  bool _matches(AgentLane lane) {
    final query = _search.text.trim().toLowerCase();
    final parent = _lanes[lane.parentLaneId];
    if (query.isNotEmpty &&
        ![
          lane.displayTitle,
          lane.task,
          lane.workspaceRoot,
          parent?.displayTitle ?? ''
        ].any((value) => value.toLowerCase().contains(query))) {
      return false;
    }
    return switch (_filter) {
      _AgentFilter.all => true,
      _AgentFilter.working => const {
          'queued',
          'running',
          'interrupt_requested',
          'cancel_requested'
        }.contains(lane.status),
      _AgentFilter.attention =>
        const {'awaiting_input', 'failed', 'interrupted'}.contains(lane.status),
      _AgentFilter.unread => lane.unreadReports > 0,
      _AgentFilter.finished =>
        const {'completed', 'cancelled'}.contains(lane.status),
    };
  }

  String _rootParentId(AgentLane lane) {
    final seen = <String>{};
    while (seen.add(lane.id) && _lanes.containsKey(lane.parentLaneId)) {
      lane = _lanes[lane.parentLaneId]!;
    }
    return lane.parentSessionId;
  }

  String _shortId(String id) => id.length <= 12
      ? id
      : '${id.substring(0, 8)}…${id.substring(id.length - 4)}';

  Future<void> _showParent(String id) => showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
            title: const Text('Parent conversation'),
            content: SelectableText(id),
            actions: [
              TextButton.icon(
                  onPressed: () async {
                    try {
                      await Clipboard.setData(ClipboardData(text: id));
                      if (context.mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(content: Text('Parent ID copied')));
                      }
                    } catch (_) {
                      if (context.mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                            content: Text(
                                'Could not copy. Select the ID above to copy it manually.')));
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
      _filter = _AgentFilter.all;
    });
    _searchFocus.requestFocus();
  }

  Widget _laneList() {
    final ordered = _orderedLanes().where(_matches).toList();
    final groups = <String, List<AgentLane>>{};
    for (final lane in ordered) {
      groups
          .putIfAbsent(_groupByParent ? _rootParentId(lane) : '', () => [])
          .add(lane);
    }
    final tokens = SonderTokens.of(context);
    return Column(children: [
      ListTile(
          title: const Text('Conversations'),
          trailing: IconButton(
              tooltip: 'Refresh conversations',
              onPressed: _refreshing ? null : () => _loadLanes(manual: true),
              icon: const Icon(Icons.refresh))),
      Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Column(children: [
            TextField(
                key: const Key('agent-search'),
                controller: _search,
                focusNode: _searchFocus,
                decoration: InputDecoration(
                    labelText: _hasMore
                        ? 'Search loaded conversations'
                        : 'Search conversations',
                    prefixIcon: const Icon(Icons.search, size: 18),
                    suffixIcon: _search.text.isEmpty
                        ? null
                        : IconButton(
                            tooltip: 'Clear search',
                            icon: const Icon(Icons.close, size: 18),
                            onPressed: () {
                              setState(() => _search.clear());
                              _searchFocus.requestFocus();
                            })),
                onChanged: (_) => setState(() {})),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(
                  child: PopupMenuButton<_AgentFilter>(
                      tooltip: 'Filter agent status',
                      onSelected: (value) => setState(() => _filter = value),
                      itemBuilder: (_) => [
                            for (final entry in _filterLabels.entries)
                              PopupMenuItem(
                                  value: entry.key, child: Text(entry.value))
                          ],
                      child: Padding(
                          padding: const EdgeInsets.symmetric(vertical: 10),
                          child: Row(children: [
                            const Icon(Icons.filter_list, size: 18),
                            const SizedBox(width: 8),
                            Flexible(
                                child: Text(_filterLabels[_filter]!,
                                    overflow: TextOverflow.ellipsis)),
                            const Icon(Icons.arrow_drop_down, size: 18)
                          ])))),
              IconButton(
                  tooltip:
                      _groupByParent ? 'Show a flat list' : 'Group by parent',
                  isSelected: _groupByParent,
                  onPressed: () =>
                      setState(() => _groupByParent = !_groupByParent),
                  icon: const Icon(Icons.account_tree_outlined, size: 18)),
            ]),
            Align(
                alignment: Alignment.centerLeft,
                child: Text(
                    '${ordered.length} of ${_lanes.length} loaded${_hasMore ? ' · more available' : ''}',
                    style: Theme.of(context).textTheme.labelSmall)),
            const SizedBox(height: 8),
          ])),
      if (_listError != null)
        Padding(
            padding: const EdgeInsets.all(12),
            child: WorkspaceNotice(
                message: _listError!,
                tone: NoticeTone.warning,
                action: TextButton(
                    onPressed: _listFailure?.settingsRequired == true &&
                            widget.onNavigate != null
                        ? () => _navigate(WorkspaceDestination.settings)
                        : () => _loadLanes(manual: true),
                    child: Text(_listFailure?.settingsRequired == true &&
                            widget.onNavigate != null
                        ? 'Open Settings'
                        : 'Retry')))),
      Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : _lanes.isEmpty
                  ? const Center(
                      child: Padding(
                          padding: EdgeInsets.all(24),
                          child: Text(
                              'Delegated agents appear here. Their conversations remain available after work finishes.')))
                  : ListView(children: [
                      if (ordered.isEmpty)
                        Padding(
                            padding: const EdgeInsets.all(20),
                            child: Column(children: [
                              const Text(
                                  'No loaded conversations match these filters.'),
                              TextButton(
                                  onPressed: _clearFilters,
                                  child: const Text('Clear filters')),
                            ])),
                      for (final group in groups.entries) ...[
                        if (group.key.isNotEmpty)
                          Padding(
                              padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
                              child: Semantics(
                                  label: 'Parent conversation ${group.key}',
                                  child: TextButton.icon(
                                      onPressed: () => _showParent(group.key),
                                      icon: const Icon(Icons.call_split,
                                          size: 14),
                                      label: Text(
                                          'Parent · ${_shortId(group.key)}',
                                          style: Theme.of(context)
                                              .textTheme
                                              .labelSmall)))),
                        for (final lane in group.value)
                          ListTile(
                              selected: lane.id == _selected,
                              contentPadding: EdgeInsets.only(
                                  left: _lanes.containsKey(lane.parentLaneId) && _groupByParent
                                      ? 28
                                      : 12,
                                  right: 12),
                              leading:
                                  Icon(_lanes.containsKey(lane.parentLaneId) ? Icons.subdirectory_arrow_right : Icons.chat_bubble_outline,
                                      size: 18),
                              title: Text(lane.displayTitle,
                                  maxLines: 2, overflow: TextOverflow.ellipsis),
                              subtitle: Text(lane.statusLabel,
                                  style: TextStyle(
                                      color: const {'failed', 'awaiting_input'}
                                              .contains(lane.status)
                                          ? tokens.danger
                                          : tokens.text2)),
                              trailing: lane.unreadReports > 0
                                  ? Semantics(
                                      label: '${lane.unreadReports} unread reports',
                                      child: Badge(label: Text('${lane.unreadReports}'), child: const Icon(Icons.mark_chat_unread_outlined, size: 18)))
                                  : null,
                              onTap: () => _select(lane.id)),
                      ],
                      if (_hasMore)
                        TextButton(
                            onPressed: _refreshing
                                ? null
                                : () => _loadLanes(more: true, manual: true),
                            child: const Text('Load more conversations')),
                    ])),
    ]);
  }

  Widget _readable(Widget child) => Align(
      alignment: Alignment.topCenter,
      child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: conversationWidth),
          child: child));

  void _goLatest(String id) {
    final scroll = _scrolls[id];
    if (scroll == null || !scroll.hasClients) return;
    scroll.animateTo(scroll.position.maxScrollExtent,
        duration: MediaQuery.disableAnimationsOf(context)
            ? Duration.zero
            : const Duration(milliseconds: 180),
        curve: Curves.easeOut);
  }

  Widget _parentContext(AgentLane lane) {
    final parent = _lanes[lane.parentLaneId];
    if (lane.parentLaneId != null) {
      return TextButton.icon(
          onPressed: () => _select(lane.parentLaneId!),
          icon: const Icon(Icons.arrow_upward, size: 14),
          label: Text(parent?.displayTitle ?? 'Parent conversation',
              overflow: TextOverflow.ellipsis));
    }
    if (lane.parentSessionId.isEmpty) return const SizedBox.shrink();
    return TextButton.icon(
        onPressed: () => _showParent(lane.parentSessionId),
        icon: const Icon(Icons.call_split, size: 14),
        label: Text('Parent conversation · ${_shortId(lane.parentSessionId)}',
            overflow: TextOverflow.ellipsis));
  }

  Widget _transcript(AgentLane lane) {
    final snapshot = _snapshots[lane.id];
    final pending = _pending[lane.id];
    final controller = _drafts.putIfAbsent(lane.id, TextEditingController.new);
    final scroll = _scrolls.putIfAbsent(lane.id, ScrollController.new);
    final needsResume =
        const {'failed', 'interrupted', 'awaiting_input'}.contains(lane.status);
    final messages = <int, AgentMessage>{};
    final history = (_events[lane.id]?.values.toList() ?? <AgentEvent>[])
      ..sort((a, b) => a.sequence.compareTo(b.sequence));
    for (final event in history) {
      if (event.type == 'lane.message') {
        messages[event.sequence] = AgentMessage.fromJson(
            {...event.payload, 'sequence': event.sequence});
      }
    }
    for (final message in snapshot?.messages ?? <AgentMessage>[]) {
      messages[message.sequence] = message;
    }
    final entries = <({int sequence, Widget widget})>[];
    for (final message in messages.values) {
      entries.add((
        sequence: message.sequence,
        widget: _entry(message.authorLabel, message.content,
            detail: message.deliveryState == 'queued'
                ? (needsResume
                    ? 'Queued · choose Resume to continue'
                    : 'Queued for the next turn')
                : message.deliveryState == 'accepted'
                    ? 'Received by agent'
                    : null)
      ));
    }
    final tools = <String, ({AgentEvent first, AgentEvent? result})>{};
    for (final event in history) {
      if (event.type == 'model.response') {
        entries.add((
          sequence: event.sequence,
          widget: _entry('Agent', event.payload['content']?.toString() ?? '')
        ));
      }
      if (event.type.startsWith('tool.')) {
        final id = event.payload['call_id']?.toString() ?? event.id;
        final key = id.isEmpty ? '${event.sequence}' : id;
        tools[key] = (
          first: tools[key]?.first ?? event,
          result: event.type == 'tool.result' ? event : tools[key]?.result
        );
      }
    }
    for (final tool in tools.entries) {
      final first = tool.value.first;
      final result = tool.value.result;
      final args = first.payload['arguments'];
      final output = result?.payload['output']?.toString() ?? '';
      final status = result == null
          ? 'Requested'
          : result.payload['success'] == false
              ? 'Failed'
              : result.payload['success'] == true
                  ? 'Completed'
                  : 'Result received';
      entries.add((
        sequence: first.sequence,
        widget: Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Card(
                child: ExpansionTile(
              key: PageStorageKey('tool-${lane.id}-${tool.key}'),
              leading: const Icon(Icons.terminal, size: 18),
              title: Text(first.payload['name']?.toString() ?? 'Tool activity'),
              subtitle: Text(status),
              childrenPadding: const EdgeInsets.all(16),
              expandedCrossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (args != null) ...[
                  const Text('Request'),
                  const SizedBox(height: 6),
                  SelectableText(
                      const JsonEncoder.withIndent('  ').convert(args),
                      style: SonderTokens.of(context).mono(12)),
                  const SizedBox(height: 12)
                ],
                if (output.isNotEmpty)
                  SelectableText(output,
                      style: SonderTokens.of(context).mono(12))
                else
                  Text(result == null
                      ? 'No result received yet.'
                      : 'No text output.')
              ],
            )))
      ));
    }
    entries.sort((a, b) => a.sequence.compareTo(b.sequence));
    final reports = _reports.values
        .where((report) => _reportParents[report.id] == lane.id)
        .toList();
    final canSend = pending == null &&
        lane.status != 'cancelled' &&
        _detailFailure?.settingsRequired != true &&
        controller.text.trim().isNotEmpty;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _readable(Padding(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            _parentContext(lane),
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  child: Tooltip(
                      message: lane.displayTitle,
                      child: Text(lane.displayTitle,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.titleLarge))),
              const SizedBox(width: 12),
              Semantics(
                  liveRegion: true, child: Chip(label: Text(lane.statusLabel))),
            ]),
            Wrap(
                spacing: 8,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  if (lane.canInterrupt)
                    TextButton.icon(
                        onPressed: pending == null
                            ? () => _command(lane.id, 'interrupt')
                            : null,
                        icon: const Icon(Icons.pause, size: 18),
                        label: const Text('Interrupt')),
                  if (lane.canResume)
                    TextButton.icon(
                        onPressed: pending == null
                            ? () => _command(lane.id, 'resume')
                            : null,
                        icon: const Icon(Icons.play_arrow, size: 18),
                        label: const Text('Resume')),
                  if (lane.canCancel)
                    TextButton(
                        onPressed: pending == null ? () => _cancel(lane) : null,
                        child: const Text('Cancel work')),
                  IconButton(
                      tooltip: 'Go to latest activity',
                      onPressed: () => _goLatest(lane.id),
                      icon: const Icon(Icons.vertical_align_bottom, size: 18)),
                ]),
            if (_detailError != null)
              WorkspaceNotice(
                  message: _detailError!,
                  tone: NoticeTone.warning,
                  action: TextButton(
                      onPressed: _detailFailure?.settingsRequired == true &&
                              widget.onNavigate != null
                          ? () => _navigate(WorkspaceDestination.settings)
                          : () => _select(lane.id),
                      child: Text(_detailFailure?.settingsRequired == true &&
                              widget.onNavigate != null
                          ? 'Open Settings'
                          : 'Retry'))),
          ]))),
      const Divider(height: 1),
      Expanded(
          child: snapshot == null
              ? Center(
                  child: _detailError == null
                      ? const CircularProgressIndicator()
                      : const Text('Conversation not loaded.'))
              : ListView(
                  key: PageStorageKey('transcript-${lane.id}'),
                  controller: scroll,
                  padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
                  children: [
                      _readable(Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            if (lane.task.isNotEmpty ||
                                lane.workspaceRoot.isNotEmpty)
                              ExpansionTile(
                                key: PageStorageKey('task-${lane.id}'),
                                title: const Text('Task and workspace'),
                                tilePadding: EdgeInsets.zero,
                                expandedCrossAxisAlignment:
                                    CrossAxisAlignment.start,
                                children: [
                                  if (lane.task.isNotEmpty)
                                    ConversationContent(content: lane.task),
                                  if (lane.workspaceRoot.isNotEmpty) ...[
                                    const SizedBox(height: 12),
                                    Text('Assigned workspace',
                                        style: Theme.of(context)
                                            .textTheme
                                            .labelLarge),
                                    SelectableText(lane.workspaceRoot,
                                        style:
                                            SonderTokens.of(context).mono(12))
                                  ],
                                  const SizedBox(height: 16)
                                ],
                              ),
                            if (snapshot.hasMore)
                              const Padding(
                                  padding: EdgeInsets.symmetric(vertical: 12),
                                  child: Text('Loading conversation history…')),
                            if (lane.error.isNotEmpty)
                              Padding(
                                  padding: const EdgeInsets.only(bottom: 16),
                                  child: WorkspaceNotice(
                                      message: lane.error,
                                      tone: NoticeTone.warning)),
                            if (entries.isEmpty)
                              const Padding(
                                  padding: EdgeInsets.symmetric(vertical: 20),
                                  child: Text(
                                      'This conversation has no messages yet.')),
                            ...entries.map((entry) => entry.widget),
                            if (reports.isNotEmpty ||
                                _reportErrors[lane.id] != null) ...[
                              const Divider(),
                              Text('Reports to parent',
                                  style:
                                      Theme.of(context).textTheme.titleMedium),
                              const SizedBox(height: 6),
                              const Text(
                                  'Marking a report read does not approve or integrate its changes.'),
                              const SizedBox(height: 12),
                            ],
                            if (_reportErrors[lane.id] != null)
                              WorkspaceNotice(
                                  message: _reportErrors[lane.id]!,
                                  tone: NoticeTone.warning,
                                  action: TextButton(
                                      onPressed: () => _loadReports(lane.id),
                                      child: const Text('Retry reports'))),
                            for (final report in reports)
                              Padding(
                                  padding: const EdgeInsets.only(bottom: 12),
                                  child: Card(
                                      child: ExpansionTile(
                                    key: PageStorageKey('report-${report.id}'),
                                    initiallyExpanded: !report.acknowledged,
                                    leading: Icon(
                                        report.acknowledged
                                            ? Icons.mark_chat_read_outlined
                                            : Icons.mark_chat_unread_outlined,
                                        size: 18),
                                    title: const Text('Report to parent'),
                                    subtitle: Text(report.acknowledged
                                        ? 'Read'
                                        : 'Unread'),
                                    childrenPadding: const EdgeInsets.fromLTRB(
                                        16, 0, 16, 16),
                                    expandedCrossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                      ConversationContent(
                                          content: report.summary),
                                      if (report.artifacts.isNotEmpty) ...[
                                        const SizedBox(height: 12),
                                        Text('Artifacts',
                                            style: Theme.of(context)
                                                .textTheme
                                                .labelLarge),
                                        for (final artifact in report.artifacts)
                                          Padding(
                                              padding:
                                                  const EdgeInsets.symmetric(
                                                      vertical: 6),
                                              child: SelectableText(artifact,
                                                  style:
                                                      SonderTokens.of(context)
                                                          .mono(12)))
                                      ],
                                      if (!report.acknowledged)
                                        TextButton(
                                            onPressed: _acknowledging
                                                    .contains(report.id)
                                                ? null
                                                : () =>
                                                    _ackReport(report, lane.id),
                                            child: const Text('Mark read'))
                                    ],
                                  ))),
                            if (_reportHasMore[lane.id] == true)
                              TextButton(
                                  onPressed: () =>
                                      _loadReports(lane.id, more: true),
                                  child: const Text('Load more reports')),
                          ])),
                    ])),
      _readable(
          Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (_commandErrors[lane.id] != null)
          _notice(_commandErrors[lane.id]!, warning: true),
        if (pending != null)
          _notice(pending.error ?? 'Sending request…',
              warning: pending.error != null,
              action: pending.error == null
                  ? null
                  : TextButton(
                      onPressed: () => _command(lane.id, pending.action),
                      child: const Text('Retry request'))),
        SafeArea(
            top: false,
            child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (needsResume)
                        Padding(
                            padding: const EdgeInsets.only(bottom: 8),
                            child: Text(
                                'Messages wait here until you choose Resume.',
                                style: Theme.of(context).textTheme.labelSmall)),
                      if (lane.status == 'cancelled')
                        const Padding(
                            padding: EdgeInsets.only(bottom: 8),
                            child: Text(
                                'This conversation was cancelled. Its messages remain available.')),
                      Row(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Expanded(
                                child: CallbackShortcuts(
                                    bindings: {
                                  const SingleActivator(
                                      LogicalKeyboardKey.enter,
                                      control: true): _sendSelected,
                                  const SingleActivator(
                                      LogicalKeyboardKey.enter,
                                      meta: true): _sendSelected,
                                },
                                    child: TextField(
                                        key: ValueKey('composer-${lane.id}'),
                                        controller: controller,
                                        focusNode: _composerFocus,
                                        enabled: pending == null &&
                                            lane.status != 'cancelled' &&
                                            _detailFailure?.settingsRequired !=
                                                true,
                                        minLines: 1,
                                        maxLines: 6,
                                        decoration: const InputDecoration(
                                            labelText: 'Message this agent',
                                            hintText:
                                                'Send a correction or follow-up'),
                                        onChanged: (_) => setState(() {})))),
                            const SizedBox(width: 8),
                            IconButton.filled(
                                tooltip: 'Send to agent',
                                onPressed: canSend ? _sendSelected : null,
                                icon: const Icon(Icons.arrow_upward)),
                          ]),
                      const SizedBox(height: 6),
                      Text(
                          'Ctrl+Enter or ⌘+Enter to send · Enter for a new line',
                          style: Theme.of(context).textTheme.labelSmall),
                    ]))),
      ])),
    ]);
  }

  Future<void> _ackReport(AgentReport report, String laneId) async {
    if (!_acknowledging.add(report.id)) return;
    setState(() {});
    try {
      await widget.api.agentAcknowledge(
        report.id,
        commandId: 'read-${report.id}',
      );
      await _loadReports(laneId);
      await _loadLanes(manual: true);
    } catch (_) {
      if (mounted) {
        setState(
          () => _reportErrors[laneId] =
              'Could not confirm the report was marked read. Retry reports to check its current state.',
        );
      }
    } finally {
      _acknowledging.remove(report.id);
      if (mounted) setState(() {});
    }
  }

  Widget _entry(String author, String content, {String? detail}) => Padding(
        padding: const EdgeInsets.only(bottom: 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(author, style: Theme.of(context).textTheme.labelLarge),
            const SizedBox(height: 6),
            ConversationContent(content: content),
            if (detail != null)
              Text(detail, style: Theme.of(context).textTheme.labelSmall),
          ],
        ),
      );
  Widget _notice(String text, {Widget? action, bool warning = false}) =>
      Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: WorkspaceNotice(
              message: text,
              tone: warning ? NoticeTone.warning : NoticeTone.info,
              action: action));

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          final wide = constraints.maxWidth >= 850;
          final lane = _lanes[_selected];
          void focusSearch() {
            if (!wide && _selected != null) {
              setState(() => _selected = null);
              _stopWatch();
            }
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (mounted) _searchFocus.requestFocus();
            });
          }

          return CallbackShortcuts(
              bindings: {
                const SingleActivator(LogicalKeyboardKey.keyF,
                    control: true, shift: true): focusSearch,
                const SingleActivator(LogicalKeyboardKey.keyF,
                    meta: true, shift: true): focusSearch,
              },
              child: Scaffold(
                appBar: AppBar(
                  title: const Text('Agents'),
                  actions: [
                    IconButton(
                        tooltip: 'Find conversation (Ctrl+Shift+F)',
                        onPressed: focusSearch,
                        icon: const Icon(Icons.search)),
                    if (widget.onNavigate != null) ...[
                      WorkspaceMenu(
                          current: WorkspaceDestination.agents,
                          onSelected: _navigate),
                      TextButton.icon(
                          onPressed: () => _navigate(WorkspaceDestination.chat),
                          icon: const Icon(Icons.chat_bubble_outline, size: 18),
                          label: const Text('Chat')),
                    ],
                  ],
                  leading: !wide && _selected != null
                      ? IconButton(
                          tooltip: 'All agent conversations',
                          icon: const Icon(Icons.arrow_back),
                          onPressed: () {
                            setState(() => _selected = null);
                            _stopWatch();
                          },
                        )
                      : Navigator.of(context).canPop()
                          ? IconButton(
                              tooltip: 'Back to chat',
                              icon: const Icon(Icons.arrow_back),
                              onPressed: () =>
                                  _navigate(WorkspaceDestination.chat))
                          : null,
                ),
                body: wide
                    ? Row(
                        children: [
                          SizedBox(width: 272, child: _laneList()),
                          const VerticalDivider(width: 1),
                          Expanded(
                            child: lane == null
                                ? const Center(
                                    child: Text('Select an agent conversation'),
                                  )
                                : _transcript(lane),
                          ),
                        ],
                      )
                    : lane == null
                        ? _laneList()
                        : _transcript(lane),
              ));
        },
      );
}
