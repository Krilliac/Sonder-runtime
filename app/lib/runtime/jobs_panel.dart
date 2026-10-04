/// Read-only jobs, fanout and compute views (plan P1-10). Each list loads
/// only when its disclosure is opened, so a phone never pays for admin reads
/// it did not ask for. 403 and 404 read as off-by-design (`– n/a`), not as
/// failures.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../ui/kit.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'runtime_rows.dart';
import 'status_word.dart';

StatusKind lifecycleStatus(String status) {
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

/// Durable jobs (`GET /v1/jobs`, admin), behind a disclosure.
class JobsList extends StatelessWidget {
  final RuntimeDataSource source;
  final DateTime? now;
  const JobsList({super.key, required this.source, this.now});

  @override
  Widget build(BuildContext context) {
    final clock = now ?? DateTime.now();
    return SettingsSection(
      title: 'Jobs',
      description: 'Durable background jobs on this server.',
      children: [
        LazyRuntimeList<JobSummary>(
          key: const Key('jobs-details'),
          title: 'Recent jobs',
          what: 'jobs',
          load: source.jobs,
          empty: 'No jobs',
          row: (job) => RuntimeRow(
            kind: lifecycleStatus(job.status),
            word: job.status.isEmpty ? null : job.status,
            title: RuntimeRowTitle(job.kind.isEmpty ? 'job' : job.kind),
            subtitle: RuntimeRowDetail(
                [
                  job.id,
                  if (job.updatedAt != null)
                    '${compactDuration(clock.difference(job.updatedAt!))} ago',
                ].join(' · '),
                mono: true),
          ),
        ),
      ],
    );
  }
}

/// Model fanout history (`GET /v1/fanout`), behind a disclosure.
class FanoutList extends StatelessWidget {
  final RuntimeDataSource source;
  const FanoutList({super.key, required this.source});

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      title: 'Model fanout',
      description: 'One prompt answered by several models at once.',
      children: [
        LazyRuntimeList<FanoutSummary>(
          key: const Key('fanout-details'),
          title: 'Recent fanout runs',
          what: 'fanout runs',
          load: source.fanoutRuns,
          empty: 'No fanout runs',
          row: (run) => RuntimeRow(
            kind: lifecycleStatus(run.status),
            word: run.status.isEmpty ? null : run.status,
            title: RuntimeRowTitle(run.id, mono: true, maxLines: 1),
            subtitle: RuntimeRowDetail([
              '${run.answered}/${run.selected} answered',
              if (run.failed > 0) '${run.failed} failed',
              if (run.running > 0) '${run.running} running',
            ].join(' · ')),
          ),
        ),
      ],
    );
  }
}

/// Compute nodes (`GET /v1/compute/nodes`, admin), behind a disclosure.
class ComputeNodesList extends StatelessWidget {
  final RuntimeDataSource source;
  const ComputeNodesList({super.key, required this.source});

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      title: 'Compute nodes',
      description: 'This PC and the peers it can place whole jobs on.',
      children: [
        LazyRuntimeList<ComputeNode>(
          key: const Key('compute-details'),
          title: 'Nodes',
          what: 'compute nodes',
          load: source.computeNodes,
          empty: 'No compute nodes configured',
          row: (node) => RuntimeRow(
            kind: node.stale && node.health == 'unknown'
                ? StatusKind.unknown
                : lifecycleStatus(node.health),
            word: node.health.isEmpty ? null : node.health,
            title: RuntimeRowTitle(node.id, mono: true, maxLines: 1),
            subtitle: RuntimeRowDetail([
              node.local ? 'this PC' : 'peer',
              if (node.activeJobs != null) '${node.activeJobs} active jobs',
              if (node.stale) 'stale',
              if (node.probeError.isNotEmpty) node.probeError,
            ].join(' · ')),
          ),
        ),
      ],
    );
  }
}

/// A disclosure that loads its rows the first time it opens, and again on
/// Refresh.
class LazyRuntimeList<T> extends StatefulWidget {
  final String title;

  /// What the rows are, for the refresh tooltip ("jobs").
  final String what;
  final Future<List<T>> Function() load;
  final String empty;
  final Widget Function(T item) row;

  const LazyRuntimeList({
    super.key,
    required this.title,
    required this.what,
    required this.load,
    required this.empty,
    required this.row,
  });

  @override
  State<LazyRuntimeList<T>> createState() => _LazyRuntimeListState<T>();
}

class _LazyRuntimeListState<T> extends State<LazyRuntimeList<T>> {
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

  List<Widget> _body() {
    final error = _error;
    if (error is SonderException &&
        (error.httpStatus == 403 || error.httpStatus == 401)) {
      return const [
        RuntimePanelNote(
            status: StatusKind.skipped,
            word: 'n/a',
            text: 'Needs an administrator account.'),
      ];
    }
    if (error is SonderException && error.httpStatus == 404) {
      return const [
        RuntimePanelNote(
            status: StatusKind.skipped,
            word: 'n/a',
            text: 'Not available on this server.'),
      ];
    }
    if (error != null) {
      return [
        RuntimePanelNote(
          status: StatusKind.fail,
          text: error is SonderException ? error.message : 'Could not load.',
          action: TextButton(onPressed: _fetch, child: const Text('Retry')),
        ),
      ];
    }
    final items = _items;
    if (items == null) {
      return [SkeletonRows(rows: 2, semanticLabel: 'Loading ${widget.what}')];
    }
    if (items.isEmpty) return [RuntimeEmptyRow(widget.empty)];
    return [for (final item in items) widget.row(item)];
  }

  @override
  Widget build(BuildContext context) {
    final loaded = _items != null || _error != null;
    return Disclosure(
      title: widget.title,
      subtitle: loaded
          ? (_items == null ? null : '${_items!.length} shown')
          : 'Loads when opened',
      trailing: loaded
          ? IconButton(
              tooltip: 'Refresh ${widget.what}',
              onPressed: _loading ? null : _fetch,
              icon: const Icon(Icons.refresh, size: 18),
            )
          : null,
      onChanged: (open) {
        if (open && _items == null && !_loading) _fetch();
      },
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: dividedRows(context, [const SizedBox.shrink(), ..._body()]),
      ),
    );
  }
}
