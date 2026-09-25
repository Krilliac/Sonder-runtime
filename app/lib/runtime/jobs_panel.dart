/// Read-only jobs, fanout and compute views (plan P1-10). Each list loads
/// only when its "Details" disclosure is opened, so the phone never pays for
/// admin reads it did not ask for. 403 and 404 read as off-by-design
/// (`– n/a`), not as failures.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'status_word.dart';
import 'work_runs_panel.dart';

StatusKind _lifecycleStatus(String status) {
  if (const {'running', 'pending', 'queued', 'started', 'active'}
      .contains(status)) {
    return StatusKind.running;
  }
  if (const {'completed', 'succeeded', 'done', 'answered', 'healthy'}
      .contains(status)) {
    return StatusKind.ok;
  }
  if (const {'failed', 'error', 'unhealthy', 'interrupted'}.contains(status)) {
    return StatusKind.fail;
  }
  if (const {'cancelled', 'skipped'}.contains(status)) {
    return StatusKind.skipped;
  }
  if (const {'degraded', 'stale'}.contains(status)) return StatusKind.warn;
  return StatusKind.unknown;
}

class JobsPanel extends StatelessWidget {
  final RuntimeDataSource source;
  final DateTime? now;
  const JobsPanel({super.key, required this.source, this.now});

  @override
  Widget build(BuildContext context) {
    final clock = now ?? DateTime.now();
    return Column(
      key: const Key('jobs-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _LazyList<JobSummary>(
          key: const Key('jobs-details'),
          title: 'Jobs',
          load: source.jobs,
          empty: 'No jobs',
          row: (job) => _Row(
            status: _lifecycleStatus(job.status),
            word: job.status.isEmpty ? null : job.status,
            text: [
              job.kind.isEmpty ? 'job' : job.kind,
              job.id,
              if (job.updatedAt != null)
                '${compactDuration(clock.difference(job.updatedAt!))} ago',
            ].join(' · '),
          ),
        ),
        _LazyList<FanoutSummary>(
          key: const Key('fanout-details'),
          title: 'Model fanout',
          load: source.fanoutRuns,
          empty: 'No fanout runs',
          row: (run) => _Row(
            status: _lifecycleStatus(run.status),
            word: run.status.isEmpty ? null : run.status,
            text: [
              run.id,
              '${run.answered}/${run.selected} answered',
              if (run.failed > 0) '${run.failed} failed',
              if (run.running > 0) '${run.running} running',
            ].join(' · '),
          ),
        ),
        _LazyList<ComputeNode>(
          key: const Key('compute-details'),
          title: 'Compute nodes',
          load: source.computeNodes,
          empty: 'No compute nodes configured',
          row: (node) => _Row(
            status: node.stale && node.health == 'unknown'
                ? StatusKind.unknown
                : _lifecycleStatus(node.health),
            word: node.health.isEmpty ? null : node.health,
            text: [
              node.id,
              node.local ? 'this PC' : 'peer',
              if (node.activeJobs != null) '${node.activeJobs} active jobs',
              if (node.stale) 'stale',
              if (node.probeError.isNotEmpty) node.probeError,
            ].join(' · '),
          ),
        ),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  final StatusKind status;
  final String? word;
  final String text;
  const _Row({required this.status, required this.text, this.word});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        RuntimeStatusWord(status, word: word, width: 116),
        Expanded(
            child: Text(text,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(12, color: tokens.text2))),
      ]),
    );
  }
}

/// A collapsed "Details" disclosure that loads its rows the first time it
/// opens, and again on Refresh.
class _LazyList<T> extends StatefulWidget {
  final String title;
  final Future<List<T>> Function() load;
  final String empty;
  final Widget Function(T item) row;
  const _LazyList({
    super.key,
    required this.title,
    required this.load,
    required this.empty,
    required this.row,
  });

  @override
  State<_LazyList<T>> createState() => _LazyListState<T>();
}

class _LazyListState<T> extends State<_LazyList<T>> {
  List<T>? _items;
  Object? _error;
  bool _loading = false;

  Future<void> _fetch() async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final items = await widget.load();
      if (!mounted) return;
      setState(() => _items = items);
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Widget _body() {
    final error = _error;
    if (error is SonderException &&
        (error.httpStatus == 403 || error.httpStatus == 401)) {
      return const RuntimePanelNote(
          status: StatusKind.skipped,
          word: 'n/a',
          text: 'Needs an administrator account.');
    }
    if (error is SonderException && error.httpStatus == 404) {
      return const RuntimePanelNote(
          status: StatusKind.skipped,
          word: 'n/a',
          text: 'Not available on this server.');
    }
    if (error != null) {
      return RuntimePanelNote(
        status: StatusKind.fail,
        text: error is SonderException ? error.message : 'Could not load.',
        action: TextButton(onPressed: _fetch, child: const Text('Retry')),
      );
    }
    final items = _items;
    if (items == null) {
      return const RuntimePanelNote(
          status: StatusKind.unknown, word: 'checking', text: 'Loading…');
    }
    if (items.isEmpty) {
      return RuntimePanelNote(status: StatusKind.note, text: widget.empty);
    }
    return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [for (final item in items) widget.row(item)]);
  }

  @override
  Widget build(BuildContext context) {
    return Theme(
      data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
      child: ExpansionTile(
        tilePadding: EdgeInsets.zero,
        childrenPadding: const EdgeInsets.only(bottom: 8),
        expandedCrossAxisAlignment: CrossAxisAlignment.start,
        title: Text('${widget.title} · Details',
            style: Theme.of(context).textTheme.labelLarge),
        trailing: _items != null || _error != null
            ? IconButton(
                tooltip: 'Refresh ${widget.title.toLowerCase()}',
                onPressed: _loading ? null : _fetch,
                icon: const Icon(Icons.refresh, size: 18),
              )
            : null,
        onExpansionChanged: (open) {
          if (open && _items == null && !_loading) _fetch();
        },
        children: [_body()],
      ),
    );
  }
}
