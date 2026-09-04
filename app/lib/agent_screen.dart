import 'dart:async';

import 'package:flutter/material.dart';

import 'agent_lanes.dart';
import 'agent_command_id.dart';
import 'api.dart';
import 'theme.dart';

class AgentScreen extends StatefulWidget {
  final SonderApi api;
  const AgentScreen({super.key, required this.api});
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

class _AgentScreenState extends State<AgentScreen> {
  final _lanes = <String, AgentLane>{};
  final _drafts = <String, TextEditingController>{};
  final _snapshots = <String, AgentSnapshot>{};
  final _events = <String, Map<int, AgentEvent>>{};
  final _pending = <String, _PendingCommand>{};
  final _commandErrors = <String, String>{};
  final _reports = <String, AgentReport>{};
  final _reportParents = <String, String>{};
  final _reportCursors = <String, int>{};
  final _reportHasMore = <String, bool>{};
  final _reportErrors = <String, String>{};
  String? _selected, _listError, _detailError;
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
    _loadLanes();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _loadLanes());
  }

  @override
  void dispose() {
    _stopWatch();
    _timer?.cancel();
    for (final controller in _drafts.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _loadLanes({bool more = false}) async {
    if (_refreshing) return;
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
      });
    } catch (_) {
      if (mounted) {
        setState(
          () => _listError =
              'Could not refresh conversations. Check your connection and server settings.',
        );
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
    });
    final generation = ++_generation;
    unawaited(_watch(id, generation));
    unawaited(_loadReports(id));
  }

  Future<void> _loadReports(String id, {bool more = false}) async {
    final lane = _lanes[id];
    if (lane == null) return;
    try {
      // Reports are addressed to the parent session, which can belong to an
      // external harness. Show only reports authored by this selected lane.
      final page = await widget.api.agentReports(lane.parentSessionId,
          cursor: more ? (_reportCursors[id] ?? 0) : 0);
      if (!mounted) return;
      setState(() {
        for (final report in page.reports) {
          if (report.laneId != id) continue;
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
    }
  }

  Future<void> _watch(String id, int generation) async {
    while (mounted && generation == _generation) {
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
        });
        unawaited(_loadReports(id));
        if (!snapshot.hasMore) {
          await _delay(const Duration(seconds: 2));
        }
      } catch (_) {
        if (!mounted || generation != _generation) return;
        setState(
          () => _detailError =
              'Connection lost. Saved conversation remains here; reconnecting…',
        );
        await _delay(const Duration(seconds: 5));
      }
    }
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

  Widget _laneList() {
    final ordered = _orderedLanes();
    return Column(
      children: [
        ListTile(
          title: const Text('Agent conversations'),
          trailing: IconButton(
            tooltip: 'Refresh conversations',
            onPressed: _loadLanes,
            icon: const Icon(Icons.refresh),
          ),
        ),
        if (_listError != null)
          Padding(
            padding: const EdgeInsets.all(12),
            child: Text(
              _listError!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ),
        Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : ordered.isEmpty
                  ? const Center(
                      child: Padding(
                        padding: EdgeInsets.all(24),
                        child: Text(
                          'Delegated agents appear here. Open a conversation to follow its work or send a message.',
                        ),
                      ),
                    )
                  : ListView.builder(
                      itemCount: ordered.length + (_hasMore ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index == ordered.length) {
                          return TextButton(
                            onPressed: () => _loadLanes(more: true),
                            child: const Text('Load more conversations'),
                          );
                        }
                        final lane = ordered[index];
                        final child = _lanes.containsKey(lane.parentLaneId);
                        return ListTile(
                          selected: lane.id == _selected,
                          contentPadding: EdgeInsets.only(
                            left: child ? 30 : 16,
                            right: 12,
                          ),
                          leading: Icon(
                            child
                                ? Icons.subdirectory_arrow_right
                                : Icons.account_tree_outlined,
                            size: 20,
                          ),
                          title: Text(
                            lane.displayTitle,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: Text(lane.statusLabel),
                          trailing: lane.unreadReports > 0
                              ? Badge(
                                  label: Text('${lane.unreadReports}'),
                                  child: const Icon(
                                    Icons.mark_chat_unread_outlined,
                                    semanticLabel: 'Unread reports',
                                  ),
                                )
                              : null,
                          onTap: () => _select(lane.id),
                        );
                      },
                    ),
        ),
      ],
    );
  }

  Widget _transcript(AgentLane lane) {
    final snapshot = _snapshots[lane.id];
    final pending = _pending[lane.id];
    final controller = _drafts.putIfAbsent(lane.id, TextEditingController.new);
    final messages = <int, AgentMessage>{};
    for (final event in _events[lane.id]?.values ?? <AgentEvent>[]) {
      if (event.type == 'lane.message') {
        messages[event.sequence] = AgentMessage.fromJson({
          ...event.payload,
          'sequence': event.sequence,
        });
      }
    }
    for (final message in snapshot?.messages ?? <AgentMessage>[]) {
      messages[message.sequence] = message;
    }
    final entries = <({int sequence, Widget widget})>[];
    for (final message in messages.values) {
      entries.add((
        sequence: message.sequence,
        widget: _entry(
          message.authorLabel,
          message.content,
          detail: message.deliveryState == 'queued'
              ? 'Queued for the next safe stopping point'
              : message.deliveryState == 'accepted'
                  ? 'Received by agent'
                  : null,
        ),
      ));
    }
    for (final event in _events[lane.id]?.values ?? <AgentEvent>[]) {
      if (event.type == 'model.response') {
        entries.add((
          sequence: event.sequence,
          widget: _entry('Agent', event.payload['content']?.toString() ?? ''),
        ));
      }
      if (event.type.startsWith('tool.')) {
        entries.add((
          sequence: event.sequence,
          widget: ExpansionTile(
            title: Text(event.payload['name']?.toString() ?? 'Tool activity'),
            subtitle: Text(event.type.split('.').last),
            children: [
              Padding(
                padding: const EdgeInsets.all(12),
                child: SelectableText(
                  event.payload['output']?.toString() ??
                      event.payload['content']?.toString() ??
                      '',
                ),
              ),
            ],
          ),
        ));
      }
    }
    entries.sort((a, b) => a.sequence.compareTo(b.sequence));
    final reports = _reports.values.where(
      (r) => _reportParents[r.id] == lane.id,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (lane.parentLaneId != null)
                TextButton.icon(
                  onPressed: () => _select(lane.parentLaneId!),
                  icon: const Icon(Icons.arrow_upward, size: 16),
                  label: Text(
                    _lanes[lane.parentLaneId]?.displayTitle ??
                        'Parent conversation',
                  ),
                ),
              Text(
                lane.displayTitle,
                style: Theme.of(context).textTheme.titleMedium,
              ),
              Semantics(liveRegion: true, child: Text(lane.statusLabel)),
              if (lane.task.isNotEmpty && lane.task != lane.title)
                Text(lane.task, maxLines: 3, overflow: TextOverflow.ellipsis),
              Wrap(
                spacing: 8,
                children: [
                  if (lane.canInterrupt)
                    TextButton.icon(
                      onPressed: pending == null
                          ? () => _command(lane.id, 'interrupt')
                          : null,
                      icon: const Icon(Icons.pause),
                      label: const Text('Interrupt'),
                    ),
                  if (lane.canResume)
                    TextButton.icon(
                      onPressed: pending == null
                          ? () => _command(lane.id, 'resume')
                          : null,
                      icon: const Icon(Icons.play_arrow),
                      label: const Text('Resume'),
                    ),
                  if (lane.canCancel)
                    TextButton(
                      onPressed: pending == null ? () => _cancel(lane) : null,
                      child: const Text('Cancel work'),
                    ),
                ],
              ),
            ],
          ),
        ),
        if (_detailError != null) _notice(_detailError!),
        if (lane.error.isNotEmpty) _notice(lane.error),
        if (_reportErrors[lane.id] != null) _notice(_reportErrors[lane.id]!),
        if (_commandErrors[lane.id] != null) _notice(_commandErrors[lane.id]!),
        Expanded(
          child: snapshot == null
              ? const Center(child: CircularProgressIndicator())
              : ListView(
                  key: PageStorageKey('transcript-${lane.id}'),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 20,
                    vertical: 12,
                  ),
                  children: [
                    if (entries.isEmpty)
                      const Text('This conversation has no messages yet.'),
                    ...entries.map((e) => e.widget),
                    if (_reportHasMore[lane.id] == true)
                      TextButton(
                          onPressed: () => _loadReports(lane.id, more: true),
                          child: const Text('Load more reports')),
                    for (final report in reports)
                      Card(
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Report to parent',
                                style: Theme.of(context).textTheme.titleSmall,
                              ),
                              SelectableText(report.summary),
                              for (final artifact in report.artifacts)
                                SelectableText(artifact),
                              if (!report.acknowledged)
                                TextButton(
                                  onPressed: () => _ackReport(report, lane.id),
                                  child: const Text('Mark read'),
                                ),
                            ],
                          ),
                        ),
                      ),
                  ],
                ),
        ),
        if (pending != null)
          _notice(
            pending.error ?? 'Sending request…',
            action: pending.error == null
                ? null
                : TextButton(
                    onPressed: () => _command(lane.id, pending.action),
                    child: const Text('Retry request'),
                  ),
          ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Expanded(
                  child: TextField(
                    key: ValueKey('composer-${lane.id}'),
                    controller: controller,
                    enabled: pending == null && lane.status != 'cancelled',
                    minLines: 1,
                    maxLines: 6,
                    decoration: const InputDecoration(
                      labelText: 'Message this agent',
                      hintText: 'Send a correction or follow-up',
                    ),
                    onChanged: (_) => setState(() {}),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(
                  tooltip: 'Send to agent',
                  onPressed: pending == null &&
                          lane.status != 'cancelled' &&
                          controller.text.trim().isNotEmpty
                      ? () => _command(
                            lane.id,
                            'messages',
                            content: controller.text.trim(),
                          )
                      : null,
                  icon: const Icon(Icons.arrow_upward),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  Future<void> _ackReport(AgentReport report, String laneId) async {
    try {
      await widget.api.agentAcknowledge(
        report.id,
        commandId: 'read-${report.id}',
      );
      await _loadReports(laneId);
      await _loadLanes();
    } catch (_) {
      if (mounted) {
        setState(
          () => _detailError = 'Could not mark the report read. Try again.',
        );
      }
    }
  }

  Widget _entry(String author, String content, {String? detail}) => Padding(
        padding: const EdgeInsets.only(bottom: 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(author, style: Theme.of(context).textTheme.labelLarge),
            const SizedBox(height: 6),
            SelectableText(
              content,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(fontFamily: SonderTheme.mono),
            ),
            if (detail != null)
              Text(detail, style: Theme.of(context).textTheme.labelSmall),
          ],
        ),
      );
  Widget _notice(String text, {Widget? action}) => Semantics(
        liveRegion: true,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Row(
            children: [
              Expanded(child: Text(text)),
              if (action != null) action,
            ],
          ),
        ),
      );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          final wide = constraints.maxWidth >= 850;
          final lane = _lanes[_selected];
          return Scaffold(
            appBar: AppBar(
              title: const Text('Agents'),
              leading: !wide && _selected != null
                  ? IconButton(
                      tooltip: 'All agent conversations',
                      icon: const Icon(Icons.arrow_back),
                      onPressed: () {
                        setState(() => _selected = null);
                        _stopWatch();
                      },
                    )
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
          );
        },
      );
}
