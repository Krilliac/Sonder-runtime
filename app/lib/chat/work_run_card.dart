import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'backend.dart';
import 'classify.dart';
import 'lines.dart';
import 'notice.dart';

export 'classify.dart' show WorkRunRef, workRunOf;

/// Replaces a long workbench turn's hand-off text while its work run is
/// still going (P0-8):
///
///     ◈ working  work run wr-7c1e… · 4m 12s of 30m budget
///        Still running on the PC. The answer will appear here.
///        [Refresh]  [Stop run…]
///
/// While the card is on screen it polls `GET /v1/work-runs/<id>` with a
/// 2 → 5 → 15 s backoff; when the run finishes, [onResolved] swaps the
/// persisted answer into the same message. The raw route instructions are
/// never shown.
class WorkRunCard extends StatefulWidget {
  final WorkRunRef run;
  final Future<WorkRunInfo> Function(String id) fetch;
  final Future<WorkRunInfo> Function(String id) cancel;
  final ValueChanged<WorkRunInfo> onResolved;

  /// Poll delays, in order; the last one repeats.
  final List<Duration> backoff;

  const WorkRunCard({
    super.key,
    required this.run,
    required this.fetch,
    required this.cancel,
    required this.onResolved,
    this.backoff = const [
      Duration(seconds: 2),
      Duration(seconds: 5),
      Duration(seconds: 15),
    ],
  });

  @override
  State<WorkRunCard> createState() => _WorkRunCardState();
}

class _WorkRunCardState extends State<WorkRunCard> with WidgetsBindingObserver {
  WorkRunInfo? _info;

  /// Seconds on screen, for runs whose start the server has not told us yet.
  int _ticks = 0;
  String _error = '';
  bool _forbidden = false;
  bool _stopping = false;
  bool _stopRequested = false;
  bool _inFlight = false;
  int _step = 0;
  Timer? _poll;
  Timer? _tick;
  bool _resolved = false;

  /// False while the app is backgrounded or a route covers the chat
  /// (TickerMode off): the card then issues no requests and no rebuilds.
  bool _appVisible = true;
  bool _tickersOn = true;

  bool get _visible => _appVisible && _tickersOn;

  @override
  void initState() {
    super.initState();
    final state = WidgetsBinding.instance.lifecycleState;
    _appVisible = state == null || state == AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    _resume(refreshNow: false);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final on = TickerMode.valuesOf(context).enabled;
    if (on == _tickersOn) return;
    _tickersOn = on;
    _visible ? _resume(refreshNow: true) : _pause();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final visible = state == AppLifecycleState.resumed;
    if (visible == _appVisible) return;
    _appVisible = visible;
    _visible ? _resume(refreshNow: true) : _pause();
  }

  void _pause() {
    _poll?.cancel();
    _tick?.cancel();
    _poll = null;
    _tick = null;
  }

  void _resume({required bool refreshNow}) {
    if (!_visible || _resolved || !mounted) return;
    _tick?.cancel();
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _ticks++);
    });
    if (refreshNow) {
      unawaited(_refresh(manual: true));
    } else {
      _schedule();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _poll?.cancel();
    _tick?.cancel();
    super.dispose();
  }

  void _schedule() {
    _poll?.cancel();
    if (_forbidden || _resolved || !_visible || !mounted) return;
    final delays = widget.backoff;
    final delay = delays[_step < delays.length ? _step : delays.length - 1];
    _step++;
    _poll = Timer(delay, _refresh);
  }

  Future<void> _refresh({bool manual = false}) async {
    if (_inFlight) return;
    _inFlight = true;
    if (manual) {
      _step = 0;
      _poll?.cancel();
    }
    try {
      final info = await widget.fetch(widget.run.id);
      if (!mounted) return;
      setState(() {
        _info = info;
        _error = '';
      });
      if (!info.isRunning) {
        _resolved = true;
        _poll?.cancel();
        _tick?.cancel();
        widget.onResolved(info);
        return;
      }
    } on SonderException catch (e) {
      if (!mounted) return;
      setState(() {
        _forbidden = e.httpStatus == 403;
        _error = e.message;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Could not refresh the work run.');
    } finally {
      _inFlight = false;
    }
    if (mounted) _schedule();
  }

  Future<void> _stop() async {
    if (_stopping || _stopRequested) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        key: const Key('work-run-stop-confirm'),
        title: const Text('Stop this work run?'),
        content: Text(
          'Work run ${widget.run.shortId} stops making changes at its next '
          'step. A model step already running finishes first.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Keep running'),
          ),
          FilledButton(
            key: const Key('work-run-stop-yes'),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Stop run'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _stopping = true);
    try {
      final info = await widget.cancel(widget.run.id);
      if (!mounted) return;
      setState(() {
        _info = info;
        _stopRequested = true;
        _error = '';
      });
      if (!info.isRunning) {
        _resolved = true;
        _poll?.cancel();
        _tick?.cancel();
        widget.onResolved(info);
        return;
      }
      _step = 0;
      _schedule();
    } on SonderException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _stopping = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final info = _info;
    final created = info?.createdAt;
    final elapsed =
        created == null ? _ticks : DateTime.now().difference(created).inSeconds;
    final budget = widget.run.budgetSeconds ??
        (info?.deadlineAt != null && info?.createdAt != null
            ? info!.deadlineAt!.difference(info.createdAt!).inSeconds
            : null);
    final budgetText = budget == null
        ? ''
        : ' of ${elapsedLabel(budget).replaceAll(' 00s', '')} budget';
    final title = 'work run ${widget.run.shortId} · '
        '${elapsedLabel(elapsed)}$budgetText';
    if (_forbidden) {
      return const ChatNotice(
        key: Key('work-run-forbidden'),
        kind: ChatStatusKind.warn,
        title: 'Work runs need a developer or admin account',
        detail: 'This turn is still running on the PC. Sign in with a '
            'developer or admin account to follow it here.',
      );
    }
    return Semantics(
      key: const Key('work-run-card'),
      container: true,
      label: 'working: $title',
      child: ChatNotice(
        kind: ChatStatusKind.running,
        title: title,
        detail: _stopRequested
            ? 'Stop requested. Waiting for the run to finish its current step.'
            : 'Still running on the PC. The answer will appear here.',
        hint: _error.isEmpty ? '' : "couldn't refresh: $_error",
        actions: [
          OutlinedButton(
            key: const Key('work-run-refresh'),
            onPressed: _inFlight ? null : () => _refresh(manual: true),
            child: const Text('Refresh'),
          ),
          OutlinedButton(
            key: const Key('work-run-stop'),
            style: OutlinedButton.styleFrom(foregroundColor: tokens.danger),
            onPressed: _stopping || _stopRequested ? null : _stop,
            child: Text(_stopRequested ? 'Stopping…' : 'Stop run…'),
          ),
        ],
      ),
    );
  }
}

/// The running work runs, each with Stop (after a confirm), for a 429
/// WORK_CAPACITY_EXHAUSTED.
Future<void> showRunningWork(
  BuildContext context, {
  required Future<List<WorkRunInfo>> Function() list,
  required Future<WorkRunInfo> Function(String id) cancel,
}) =>
    showDialog<void>(
      context: context,
      builder: (_) => _RunningWorkDialog(list: list, cancel: cancel),
    );

class _RunningWorkDialog extends StatefulWidget {
  final Future<List<WorkRunInfo>> Function() list;
  final Future<WorkRunInfo> Function(String id) cancel;
  const _RunningWorkDialog({required this.list, required this.cancel});

  @override
  State<_RunningWorkDialog> createState() => _RunningWorkDialogState();
}

class _RunningWorkDialogState extends State<_RunningWorkDialog> {
  List<WorkRunInfo>? _runs;
  String _error = '';
  final Set<String> _stopping = <String>{};

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final runs = await widget.list();
      if (!mounted) return;
      setState(() => _runs = runs.where((r) => r.isRunning).toList());
    } on SonderException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (_) {
      if (mounted) setState(() => _error = 'Could not list work runs.');
    }
  }

  Future<void> _stop(WorkRunInfo run) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Stop this work run?'),
        content: Text('Work run ${WorkRunRef(run.id).shortId} stops making '
            'changes at its next step.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Keep running')),
          FilledButton(
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('Stop run')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() => _stopping.add(run.id));
    try {
      await widget.cancel(run.id);
    } on SonderException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final runs = _runs;
    return AlertDialog(
      key: const Key('running-work'),
      title: const Text('Running work'),
      content: SizedBox(
        width: 420,
        child: runs == null && _error.isEmpty
            ? const LinearProgressIndicator()
            : Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (_error.isNotEmpty)
                    ChatNotice(kind: ChatStatusKind.warn, title: _error),
                  if (runs != null && runs.isEmpty)
                    const ChatNotice(
                        kind: ChatStatusKind.note, title: 'No work runs'),
                  for (final run in runs ?? const <WorkRunInfo>[])
                    Row(children: [
                      Expanded(
                        child: Text(
                          '◈ working  ${WorkRunRef(run.id).shortId}',
                          style: tokens.mono(12, color: tokens.text),
                        ),
                      ),
                      TextButton(
                        onPressed: _stopping.contains(run.id)
                            ? null
                            : () => _stop(run),
                        child: Text(
                            _stopping.contains(run.id) ? 'Stopping…' : 'Stop…'),
                      ),
                    ]),
                ],
              ),
      ),
      actions: [
        TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Close')),
      ],
    );
  }
}
