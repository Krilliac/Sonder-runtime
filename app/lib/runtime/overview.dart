/// "What is running now": the Overview page of Runtime (plan P1-5, §2.4).
/// Every tile leads with a glyph and a word, so colour never carries status
/// alone; off-by-design reads `– off`, not red. Tiles open the page that
/// owns them.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import 'runtime_data.dart';
import 'status_word.dart';

/// One overview fact, computed from the snapshots the screen already holds.
class OverviewRow {
  final StatusKind status;
  final String? word;
  final String label;

  /// The whole fact on one line (what a screen reader hears).
  final String value;

  /// The tile's big figure ("3 running") and the line under it.
  final String headline;
  final String detail;

  /// The Runtime category that owns this fact.
  final String category;

  /// 0..1 for facts that read as a meter (context use), with its label.
  final double? meter;
  final String? meterLabel;
  final String? actionLabel;
  final VoidCallback? onAction;

  const OverviewRow({
    required this.status,
    required this.label,
    required this.value,
    String? headline,
    String? detail,
    this.category = 'overview',
    this.word,
    this.meter,
    this.meterLabel,
    this.actionLabel,
    this.onAction,
  })  : headline = headline ?? value,
        detail = detail ?? '';
}

/// One line of the recent-activity list.
class ActivityLine {
  final String time;
  final StatusKind status;
  final String word;
  final String text;
  const ActivityLine(this.time, this.status, this.word, this.text);
}

String _two(int value) => value.toString().padLeft(2, '0');

String clockLabel(DateTime time) => '${_two(time.hour)}:${_two(time.minute)}';

/// `4m`, `1h 12m`, `38s`: the REPL's compact duration words.
String compactDuration(Duration duration) {
  final seconds = duration.inSeconds;
  if (seconds < 60) return '${seconds < 0 ? 0 : seconds}s';
  final minutes = duration.inMinutes;
  if (minutes < 60) return '${minutes}m';
  final hours = duration.inHours;
  final rest = minutes - hours * 60;
  return rest == 0 ? '${hours}h' : '${hours}h ${rest}m';
}

/// `2.1k`, `812`, `1.2M`: token counts for tiles and meters.
String compactCount(num value) {
  if (value.abs() < 1000) return value.round().toString();
  if (value.abs() < 1000000) {
    final k = value / 1000;
    return '${k.toStringAsFixed(k >= 100 ? 0 : 1)}k';
  }
  final m = value / 1000000;
  return '${m.toStringAsFixed(m >= 100 ? 0 : 1)}M';
}

String serverLabel(String serverUrl) {
  final uri = Uri.tryParse(serverUrl.trim());
  if (uri == null || uri.host.isEmpty) return serverUrl.trim();
  return uri.hasPort ? '${uri.host}:${uri.port}' : uri.host;
}

/// The status of one execution-feed event, from its outcome or its phase.
StatusKind executionEventStatus(ExecutionFeedEvent event) {
  if (event.ok == true) return StatusKind.ok;
  if (event.ok == false) return StatusKind.fail;
  final state = '${event.phase} ${event.responseStatus}'.toLowerCase();
  if (state.contains('refus')) return StatusKind.refused;
  if (state.contains('fail') || state.contains('error')) {
    return StatusKind.fail;
  }
  if (state.contains('cancel')) return StatusKind.skipped;
  if (state.contains('run') ||
      state.contains('start') ||
      state.contains('active')) {
    return StatusKind.running;
  }
  if (state.contains('complet') ||
      state.contains('done') ||
      state.contains('finish') ||
      state.contains('succe')) {
    return StatusKind.ok;
  }
  return StatusKind.note;
}

/// The word for an execution event's status: `done` for a finished one.
String executionEventWord(StatusKind status) => switch (status) {
      StatusKind.ok => 'done',
      StatusKind.skipped => 'cancelled',
      _ => status.runtimeWord,
    };

/// The feed's newest [limit] events, newest first.
List<ActivityLine> recentActivity(ExecutionFeed? feed, {int limit = 5}) {
  if (feed == null) return const [];
  final events = [...feed.events]..sort((a, b) => b.seq.compareTo(a.seq));
  return [
    for (final event in events.take(limit))
      () {
        final status = executionEventStatus(event);
        final parts = <String>[
          event.summary.isNotEmpty ? event.summary : event.kind,
          if (event.elapsedMs > 0)
            '${(event.elapsedMs / 1000).toStringAsFixed(1)}s',
        ];
        return ActivityLine(
          event.timestamp == null
              ? '--:--'
              : clockLabel(event.timestamp!.toLocal()),
          status,
          executionEventWord(status),
          parts.join(' · '),
        );
      }(),
  ];
}

/// Builds the overview facts. Pure, so the table is testable without
/// pumping. Facts that need a status snapshot appear only once there is one.
List<OverviewRow> overviewRows({
  required String serverUrl,
  required SystemInfo? info,
  required bool offline,
  String? serverError,
  bool loading = false,
  List<WorkRun>? workRuns,
  Object? workRunsError,
  ApprovalsPage? approvals,
  Object? approvalsError,
  DateTime? now,
  VoidCallback? onOpenWorkRuns,
  VoidCallback? onReviewApprovals,
}) {
  final host = serverLabel(serverUrl);
  final clock = now ?? DateTime.now();
  final rows = <OverviewRow>[];

  if (offline) {
    rows.add(OverviewRow(
        status: StatusKind.fail,
        label: 'Server',
        value: "Can't reach $host",
        headline: 'Offline',
        detail: "Can't reach $host",
        category: 'server'));
  } else if (info == null && serverError != null && serverError.isNotEmpty) {
    rows.add(OverviewRow(
        status: StatusKind.fail,
        label: 'Server',
        value: serverError,
        headline: 'Error',
        detail: serverError,
        category: 'server'));
  } else if (info == null) {
    rows.add(OverviewRow(
        status: StatusKind.unknown,
        word: loading ? 'checking' : null,
        label: 'Server',
        value: loading ? 'Connecting to $host…' : 'No status from $host yet',
        headline: loading ? 'Connecting…' : 'No status',
        detail: host,
        category: 'server'));
  } else {
    final summary = info.status.split('\n').first.trim();
    rows.add(OverviewRow(
        status: StatusKind.ok,
        label: 'Server',
        value: [host, if (summary.isNotEmpty) summary].join(' · '),
        headline: 'Connected',
        detail: [host, if (summary.isNotEmpty) summary].join(' · '),
        category: 'server'));
  }

  if (info != null) {
    final models =
        info.models.map((m) => m.id).where((id) => id.isNotEmpty).toList();
    final caps = info.operationalCapabilities;
    final pool = caps != null && caps.workerCount > 0
        ? 'pool ${caps.healthyWorkerCount} of ${caps.workerCount} workers'
        : '';
    final modelText = models.isEmpty
        ? 'none reported'
        : models.length <= 2
            ? models.join(', ')
            : '${models.take(2).join(', ')} +${models.length - 2}';
    final degraded = caps != null &&
        caps.workerCount > 0 &&
        caps.healthyWorkerCount < caps.workerCount;
    rows.add(OverviewRow(
        status: models.isEmpty
            ? StatusKind.warn
            : degraded
                ? StatusKind.warn
                : StatusKind.ok,
        label: 'Models',
        value: [modelText, if (pool.isNotEmpty) pool].join(' · '),
        headline: models.isEmpty
            ? 'None reported'
            : '${models.length} model${models.length == 1 ? '' : 's'}',
        detail: [modelText, if (pool.isNotEmpty) pool].join(' · '),
        category: 'models'));

    final context = info.context;
    if (context != null && context.contextLimit > 0) {
      final status = context.status;
      rows.add(OverviewRow(
          status: status == 'hot' || status == 'warm'
              ? StatusKind.warn
              : StatusKind.ok,
          word: status == 'hot' ? 'needs you' : null,
          label: 'Context',
          value: '${compactCount(context.estimatedTokens)} of '
              '${compactCount(context.contextLimit)} tokens',
          headline: '${compactCount(context.estimatedTokens)} / '
              '${compactCount(context.contextLimit)}',
          detail: context.title.isNotEmpty
              ? context.title
              : (context.session.isNotEmpty ? context.session : 'this session'),
          meter: (context.contextPercent / 100).clamp(0.0, 1.0),
          meterLabel: '${context.contextPercent.round()}%',
          category: 'models'));
    }
  }

  if (approvalsError is SonderException &&
      const {401, 403}.contains(approvalsError.httpStatus)) {
    rows.add(const OverviewRow(
        status: StatusKind.skipped,
        word: 'n/a',
        label: 'Approvals',
        value: 'need a developer or admin account',
        headline: 'Not available',
        detail: 'Needs a developer or admin account',
        category: 'permissions'));
  } else if (approvals != null) {
    if (!approvals.supported) {
      rows.add(const OverviewRow(
          status: StatusKind.skipped,
          word: 'n/a',
          label: 'Approvals',
          value: 'approve from the console (/approvals)',
          headline: 'Console only',
          detail: 'approve from the console (/approvals)',
          category: 'permissions'));
    } else if (approvals.pending.isNotEmpty) {
      final n = approvals.pending.length;
      final tools = {
        for (final item in approvals.pending)
          if (item.tool.isNotEmpty) item.tool,
      }.toList();
      rows.add(OverviewRow(
          status: StatusKind.warn,
          word: 'needs you',
          label: 'Approvals',
          value: '$n call${n == 1 ? '' : 's'} waiting',
          headline: '$n waiting',
          detail: tools.isEmpty
              ? 'Refused calls to review'
              : [
                  ...tools.take(3),
                  if (tools.length > 3) '+${tools.length - 3}',
                ].join(' · '),
          category: 'permissions',
          actionLabel: 'Review',
          onAction: onReviewApprovals));
    } else {
      rows.add(OverviewRow(
          status: StatusKind.ok,
          label: 'Approvals',
          value: approvals.open.isEmpty
              ? 'none waiting'
              : 'none waiting · ${approvals.open.length} open',
          headline: 'None waiting',
          detail: approvals.open.isEmpty
              ? 'Nothing to review'
              : '${approvals.open.length} approved once',
          category: 'permissions',
          actionLabel: approvals.open.isEmpty ? null : 'Review',
          onAction: approvals.open.isEmpty ? null : onReviewApprovals));
    }
  }

  if (workRunsError is SonderException && workRunsError.httpStatus == 403) {
    rows.add(const OverviewRow(
        status: StatusKind.skipped,
        word: 'n/a',
        label: 'Work runs',
        value: 'need a developer or admin account',
        headline: 'Not available',
        detail: 'Needs a developer or admin account',
        category: 'activity'));
  } else if (workRuns != null) {
    final running = workRuns.where((run) => run.isRunning).toList();
    if (running.isEmpty) {
      rows.add(OverviewRow(
          status: StatusKind.note,
          label: 'Work runs',
          value: workRuns.isEmpty
              ? 'No work runs'
              : 'none running · ${workRuns.length} recent',
          headline: 'None running',
          detail: workRuns.isEmpty
              ? 'Hand work off from Chat'
              : '${workRuns.length} recent',
          category: 'activity',
          actionLabel: workRuns.isEmpty ? null : 'Open',
          onAction: workRuns.isEmpty ? null : onOpenWorkRuns));
    } else {
      final first = running.first;
      final elapsed = compactDuration(first.elapsed(clock) ?? Duration.zero);
      final budget = first.budget;
      rows.add(OverviewRow(
          status: StatusKind.running,
          label: 'Work runs',
          value: '${running.length} running · ${first.shortId} $elapsed',
          headline: '${running.length} running',
          detail: [
            first.shortId,
            budget == null ? elapsed : '$elapsed of ${compactDuration(budget)}',
          ].join(' · '),
          category: 'activity',
          actionLabel: 'Open',
          onAction: onOpenWorkRuns));
    }
  } else if (workRunsError != null) {
    rows.add(const OverviewRow(
        status: StatusKind.unknown,
        label: 'Work runs',
        value: 'could not load',
        headline: 'Unknown',
        detail: 'Could not load work runs',
        category: 'activity'));
  }

  if (info != null) {
    final autopilot = info.autopilot;
    final active = autopilot?.activeRuns ?? 0;
    final resumable = autopilot?.resumableRuns ?? 0;
    final objective = autopilot?.latest?.objective ?? '';
    rows.add(OverviewRow(
        status: active > 0
            ? StatusKind.running
            : resumable > 0
                ? StatusKind.warn
                : StatusKind.skipped,
        label: 'Autopilot',
        value: active > 0
            ? '$active running'
            : resumable > 0
                ? '$resumable paused, resumable'
                : 'off',
        headline: active > 0
            ? '$active running'
            : resumable > 0
                ? '$resumable paused'
                : 'Off',
        detail: objective.isNotEmpty
            ? objective
            : resumable > 0
                ? 'Resume from Activity'
                : 'No goal running',
        category: 'activity'));

    final agents = info.agents;
    final activeAgents = agents?.activeAgents ?? 0;
    final interrupted = agents?.interruptedAgents ?? 0;
    final slots = agents?.capacity?.workerSlots ?? 0;
    rows.add(OverviewRow(
        status: activeAgents > 0
            ? StatusKind.running
            : interrupted > 0
                ? StatusKind.warn
                : StatusKind.note,
        label: 'Agents',
        value: activeAgents > 0
            ? '$activeAgents running'
            : interrupted > 0
                ? '$interrupted interrupted'
                : 'none running',
        headline: activeAgents > 0
            ? '$activeAgents running'
            : interrupted > 0
                ? '$interrupted interrupted'
                : 'None running',
        detail: interrupted > 0 && activeAgents == 0
            ? 'Retry from Activity'
            : [
                if (slots > 0) '$slots worker slots',
                if ((agents?.totalAgents ?? 0) > 0)
                  '${agents!.totalAgents} total',
              ].join(' · '),
        category: 'activity'));

    final learning = info.learningHealth;
    if (learning != null) {
      final status = learning.status;
      rows.add(OverviewRow(
          status: status == 'healthy'
              ? StatusKind.ok
              : status == 'attention'
                  ? StatusKind.warn
                  : status == 'watch'
                      ? StatusKind.warn
                      : StatusKind.note,
          word: status == 'attention' ? 'needs you' : null,
          label: 'Learning',
          value: '${learning.lessons} lessons · '
              '${learning.outcomeCoveragePercent.toStringAsFixed(0)}% grounded',
          headline: '${learning.lessons} lessons',
          detail:
              '${learning.outcomeCoveragePercent.toStringAsFixed(0)}% grounded'
              '${learning.reviewedOutcomes > 0 ? ' · ${learning.reviewedPositivePercent.toStringAsFixed(0)}% judged good by callers' : ''}',
          category: 'memory'));
    }
  }
  return rows;
}

/// The overview tiles, in a fixed order so the grid never reflows as facts
/// arrive. A fact with no data yet reads "—", without a status.
const overviewTileOrder = <(String, IconData, String)>[
  ('Server', Icons.dns_outlined, 'server'),
  ('Models', Icons.memory_outlined, 'models'),
  ('Context', Icons.data_usage_outlined, 'models'),
  ('Approvals', Icons.fact_check_outlined, 'permissions'),
  ('Work runs', Icons.pending_actions_outlined, 'activity'),
  ('Agents', Icons.hub_outlined, 'activity'),
  ('Autopilot', Icons.route_outlined, 'activity'),
  ('Learning', Icons.school_outlined, 'memory'),
];

/// The Overview page: status tiles that open their page, then the newest
/// execution events.
class RuntimeOverview extends StatelessWidget {
  final List<OverviewRow> rows;
  final List<ActivityLine> activity;

  /// Whether the facts are still arriving (tiles without data shimmer).
  final bool loading;

  /// Opens a category ("activity", "permissions").
  final ValueChanged<String>? onOpen;

  const RuntimeOverview({
    super.key,
    required this.rows,
    this.activity = const [],
    this.loading = false,
    this.onOpen,
  });

  @override
  Widget build(BuildContext context) {
    final byLabel = {for (final row in rows) row.label: row};
    final tiles = <Widget>[];
    for (final (label, icon, category) in overviewTileOrder) {
      final row = byLabel[label];
      if (row == null) {
        tiles.add(_PlaceholderTile(
            label: label,
            icon: icon,
            loading: loading,
            onTap: onOpen == null ? null : () => onOpen!(category)));
        continue;
      }
      tiles.add(StatTile(
        key: Key('overview-tile-${label.toLowerCase().replaceAll(' ', '-')}'),
        label: label,
        icon: icon,
        kind: row.status,
        word: row.word ?? row.status.runtimeWord,
        value: row.headline,
        detail: row.detail.isEmpty ? null : row.detail,
        meter: row.meter == null
            ? null
            : Meter(
                value: row.meter!,
                label: 'Used',
                valueLabel: row.meterLabel ?? ''),
        onTap: onOpen == null ? null : () => onOpen!(row.category),
      ));
    }
    return Column(
      key: const Key('runtime-overview'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        StatGrid(minTileWidth: 188, children: tiles),
        RecentActivityCard(
          lines: activity,
          onAll: onOpen == null ? null : () => onOpen!('activity'),
        ),
      ],
    );
  }
}

class _PlaceholderTile extends StatelessWidget {
  final String label;
  final IconData icon;
  final bool loading;
  final VoidCallback? onTap;

  const _PlaceholderTile({
    required this.label,
    required this.icon,
    required this.loading,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    if (!loading) {
      return StatTile(
        label: label,
        icon: icon,
        value: '—',
        detail: 'Not reported',
        onTap: onTap,
      );
    }
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Semantics(
      label: '$label: loading',
      child: Container(
        decoration: BoxDecoration(
          color: tokens.panel,
          borderRadius: BorderRadius.circular(SonderRadius.card),
          border: Border.all(color: tokens.hairline),
        ),
        padding: const EdgeInsets.all(SonderSpace.lg),
        child: ExcludeSemantics(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(children: [
                Icon(icon, size: 16, color: tokens.text2),
                const SizedBox(width: SonderSpace.sm),
                Text(label, style: text.labelMedium),
              ]),
              const SizedBox(height: SonderSpace.md),
              const Skeleton(width: 96, height: 20),
              const SizedBox(height: SonderSpace.sm),
              const Skeleton(width: 140, height: 12),
            ],
          ),
        ),
      ),
    );
  }
}

/// The newest execution events: time, status word and what happened.
class RecentActivityCard extends StatelessWidget {
  final List<ActivityLine> lines;
  final VoidCallback? onAll;

  const RecentActivityCard({super.key, required this.lines, this.onAll});

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      title: 'Recent activity',
      trailing: onAll == null || lines.isEmpty
          ? null
          : TextButton(onPressed: onAll, child: const Text('All')),
      children: lines.isEmpty
          ? const [
              _ActivityRowEmpty(),
            ]
          : [for (final line in lines) _ActivityRow(line)],
    );
  }
}

class _ActivityRowEmpty extends StatelessWidget {
  const _ActivityRowEmpty();

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Row(children: [
        const RuntimeStatusWord(StatusKind.note, width: 104),
        Expanded(
          child: Text('No recent activity',
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(color: tokens.text2)),
        ),
      ]),
    );
  }
}

class _ActivityRow extends StatelessWidget {
  final ActivityLine line;
  const _ActivityRow(this.line);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final time = Text(line.time, style: tokens.mono(12, color: tokens.muted));
    final mark = StatusMark(line.status, word: line.word, size: 12);
    final body = Text(line.text,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: text.bodyMedium?.copyWith(color: tokens.text));
    return Semantics(
      container: true,
      label: '${line.time}, ${line.word}, ${line.text}',
      excludeSemantics: true,
      child: Container(
        constraints: const BoxConstraints(minHeight: 44),
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.lg, vertical: SonderSpace.md),
        child: LayoutBuilder(builder: (context, constraints) {
          if (constraints.maxWidth < 420) {
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(children: [
                  SizedBox(width: 52, child: time),
                  Flexible(child: mark),
                ]),
                const SizedBox(height: SonderSpace.xxs),
                body,
              ],
            );
          }
          return Row(
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: [
              SizedBox(width: 52, child: time),
              SizedBox(width: 104, child: mark),
              Expanded(child: body),
            ],
          );
        }),
      ),
    );
  }
}
