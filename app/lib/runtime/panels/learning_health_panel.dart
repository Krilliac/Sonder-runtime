part of '../runtime_screen.dart';

class _LearningHealthPanel extends StatelessWidget {
  final LearningHealthInfo health;

  const _LearningHealthPanel({required this.health});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final statusColor = _statusColor(context);
    final yieldText = health.distillationYield == null
        ? 'Yield building'
        : '${health.distillationYield!.toStringAsFixed(3)} lesson / positive';
    final sources = health.lessonSources.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final signals = health.signals.take(6).toList();
    final issueCount = health.quality.issueCount;
    return Column(
      key: const Key('learning-health-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Chip(
              avatar: Icon(
                _statusIcon(),
                size: 18,
                color: statusColor,
              ),
              label: Text('Learning ${health.status}'),
            ),
            Chip(
              avatar: const Icon(Icons.school_outlined, size: 18),
              label: Text('${health.lessons} lessons'),
            ),
            Chip(
              avatar: const Icon(Icons.verified_outlined, size: 18),
              label: Text('${health.outcomes} outcomes'),
            ),
            Chip(
              avatar: const Icon(Icons.science_outlined, size: 18),
              label: Text(yieldText),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _MeterBar(
          label: 'Grounded',
          percent: health.outcomeCoveragePercent,
          detail:
              '${health.outcomeInteractions}/${health.interactions} interactions have outcomes',
          color: statusColor,
        ),
        const SizedBox(height: 10),
        // Shows CALLER-JUDGED work, not the blended rate. The blend is
        // dominated by autograded outcomes -- the runtime marking its own
        // curriculum -- so on this store it reads 96% where delegated work
        // succeeds 53% of the time. A near-full green bar beside an
        // "attention" status is the most misleading thing this screen could
        // show, and this is the surface a non-CLI user actually looks at.
        _MeterBar(
          label: 'Caller-judged',
          percent: health.reviewedPositivePercent,
          detail: '${health.reviewedOutcomes} judged by a caller  |  '
              'autograded ${health.autogradedPositivePercent.toStringAsFixed(1)}% '
              'of ${health.autogradedOutcomes} (self-marked)',
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Embedded',
          percent: health.quality.embeddingPercent,
          // Blob presence overstated coverage when corrupt or degenerate
          // vectors existed, so the caption now uses the bar's valid set.
          detail: '${health.quality.embeddingPercent.toStringAsFixed(1)}% of '
              '${health.lessons} lessons have valid embeddings',
        ),
        const SizedBox(height: 12),
        Text('Lesson provenance',
            style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: sources.isEmpty
              ? const [Chip(label: Text('No lessons yet'))]
              : sources
                  .map(
                    (entry) => Chip(
                      avatar: Icon(
                        entry.key == 'interaction'
                            ? Icons.link_outlined
                            : Icons.auto_awesome_outlined,
                        size: 17,
                      ),
                      label: Text('${entry.key}  ${entry.value}'),
                    ),
                  )
                  .toList(growable: false),
        ),
        if (signals.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text('Outcome signals',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 6),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: signals
                .map(
                  (signal) => Tooltip(
                    message:
                        'Average reward ${signal.averageReward.toStringAsFixed(2)}',
                    child: Chip(
                      avatar: Icon(
                        signal.good
                            ? Icons.thumb_up_alt_outlined
                            : Icons.thumb_down_alt_outlined,
                        size: 16,
                        color: signal.good ? cs.primary : cs.error,
                      ),
                      label: Text(
                        '${signal.signal.replaceAll('_', ' ')}  ${signal.count}',
                      ),
                    ),
                  ),
                )
                .toList(growable: false),
          ),
        ],
        const SizedBox(height: 12),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(11),
          decoration: BoxDecoration(
            color: issueCount == 0
                ? cs.primaryContainer.withValues(alpha: 0.42)
                : cs.errorContainer,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                issueCount == 0
                    ? Icons.shield_outlined
                    : Icons.warning_amber_outlined,
                size: 18,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  issueCount == 0
                      ? 'Memory hygiene is clean: no duplicate, embedding, index, source, or privacy defects.'
                      : _issueText(),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 7),
        Text(
          'Inspect the exact report with /learning; review rows with /quality.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }

  String _issueText() {
    final q = health.quality;
    final parts = <String>[
      if (q.duplicateRows > 0) '${q.duplicateRows} duplicate rows',
      if (q.missingEmbeddings > 0) '${q.missingEmbeddings} missing embeddings',
      if (q.vagueLessons > 0) '${q.vagueLessons} vague lessons',
      if (q.privacyFlags > 0) '${q.privacyFlags} privacy flags',
      if (q.missingSources > 0) '${q.missingSources} missing sources',
      if (q.missingFts + q.orphanFts > 0)
        '${q.missingFts + q.orphanFts} search-index defects',
      if (q.embeddingDefects > 0)
        '${q.embeddingDefects} invalid or mismatched embeddings',
    ];
    return 'Memory hygiene needs review: ${parts.join(', ')}.';
  }

  IconData _statusIcon() {
    if (health.status == 'healthy') return Icons.check_circle_outline;
    if (health.status == 'attention') return Icons.error_outline;
    if (health.status == 'watch') return Icons.visibility_outlined;
    return Icons.hourglass_top_outlined;
  }

  Color _statusColor(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    if (health.status == 'healthy') return cs.primary;
    if (health.status == 'attention') return cs.error;
    if (health.status == 'watch') return Colors.amber.shade800;
    return cs.secondary;
  }
}
