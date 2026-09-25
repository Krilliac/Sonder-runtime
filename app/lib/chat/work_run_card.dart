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

class _WorkRunCardState extends State<WorkRunCard> {
  final DateTime _mountedAt = DateTime.now();
  WorkRunInfo? _info;
  String _error = '';
  bool _forbidden = false;
  bool _stopping = false;
  bool _stopRequested = false;
  bool _inFlight = false;
  int _step = 0;
  Timer? _poll;
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    _schedule();
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    _tick?.cancel();
    super.dispose();
  }

  void _schedule() {
    _poll?.cancel();
    if (_forbidden || !mounted) return;
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
    final started = info?.createdAt ?? _mountedAt;
    final elapsed = DateTime.now().difference(started).inSeconds;
    final budget = widget.run.budgetSeconds ??
        (info?.deadlineAt != null && info?.createdAt != null
            ? info!.deadlineAt!.difference(info.createdAt!).inSeconds
            : null);
    final budgetText =
        budget == null ? '' : ' of ${elapsedLabel(budget).replaceAll(' 00s', '')} budget';
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
