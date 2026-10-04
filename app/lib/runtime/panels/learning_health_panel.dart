part of '../runtime_screen.dart';

/// Learning quality: how much of what the runtime learned is grounded in
/// real outcomes, and whether its memory is clean.
///
/// The honest number is caller-judged work. The blended positive rate is
/// dominated by autograded outcomes (the runtime marking its own
/// curriculum), so it reads 96% where delegated work succeeds 53% of the
/// time; it is never shown on its own, and the autograded rate is labelled
/// self-marked.
class _LearningHealthPanel extends StatelessWidget {
  final _RuntimeScreenState s;
  final LearningHealthInfo health;

  const _LearningHealthPanel({required this.s, required this.health});

  @override
  Widget build(BuildContext context) {
    final kind = switch (health.status) {
      'healthy' => StatusKind.ok,
      'attention' => StatusKind.warn,
      'watch' => StatusKind.warn,
      _ => StatusKind.note,
    };
    final yieldText = health.distillationYield == null
        ? 'building'
        : '${health.distillationYield!.toStringAsFixed(3)} per positive';
    final sources = health.lessonSources.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final signals = health.signals.take(6).toList();
    final issueCount = health.quality.issueCount;
    final reportBusy = s._busy('learning');
    return Column(
      key: const Key('learning-health-panel'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(
          title: 'Learning quality',
          description: 'How much of what was learned rests on real outcomes.',
          trailing: StatusPill(kind, word: health.status),
          children: [
            RuntimeStatStrip([
              RuntimeStat('Lessons', '${health.lessons}'),
              RuntimeStat('Outcomes', '${health.outcomes}'),
              RuntimeStat('Facts', '${health.facts}'),
              RuntimeStat('Distillation yield', yieldText),
            ]),
            RuntimeCardBody(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Meter(
                    value: health.outcomeCoveragePercent / 100,
                    label: 'Grounded',
                    valueLabel:
                        '${health.outcomeCoveragePercent.toStringAsFixed(1)}%',
                    higherIsBetter: true,
                    warnAt: 0.4,
                    dangerAt: 0.7,
                  ),
                  _MeterCaption(
                      '${health.outcomeInteractions} of ${health.interactions} '
                      'interactions have outcomes'),
                  const SizedBox(height: SonderSpace.lg),
                  Meter(
                    value: health.reviewedPositivePercent / 100,
                    label: 'Caller-judged',
                    valueLabel:
                        '${health.reviewedPositivePercent.toStringAsFixed(1)}%',
                    higherIsBetter: true,
                    warnAt: 0.4,
                    dangerAt: 0.7,
                  ),
                  _MeterCaption(
                    '${health.reviewedOutcomes} judged by a caller. Autograded '
                    '${health.autogradedPositivePercent.toStringAsFixed(1)}% of '
                    '${health.autogradedOutcomes} (self-marked, not a quality '
                    'figure).',
                  ),
                  const SizedBox(height: SonderSpace.lg),
                  Meter(
                    value: health.quality.embeddingPercent / 100,
                    label: 'Embedded',
                    valueLabel:
                        '${health.quality.embeddingPercent.toStringAsFixed(1)}%',
                    higherIsBetter: true,
                    warnAt: 0.25,
                    dangerAt: 0.75,
                  ),
                  _MeterCaption('Share of the ${health.lessons} lessons with '
                      'valid embeddings'),
                ],
              ),
            ),
            if (issueCount == 0)
              const StatusValueRow(
                key: Key('learning-hygiene'),
                kind: StatusKind.ok,
                label: 'Memory hygiene',
                value: 'Clean: no duplicate, embedding, index, source or '
                    'privacy defects.',
              )
            else
              Padding(
                key: const Key('learning-hygiene'),
                padding: const EdgeInsets.all(SonderSpace.lg),
                child: WorkspaceNotice(
                  kind: StatusKind.warn,
                  title: 'Memory hygiene needs review',
                  detail: _issueText(),
                  framed: false,
                  liveRegion: false,
                ),
              ),
            SettingRow(
              label: 'Details',
              description: 'The exact learning report, and the rows that '
                  'need review.',
              trailing: Wrap(spacing: SonderSpace.sm, children: [
                AsyncActionButton(
                  label: 'Learning report',
                  busyLabel: 'Reading…',
                  doneLabel: null,
                  busy: reportBusy && s._learningCommand == '/learning',
                  onPressed:
                      reportBusy ? null : () => s._learningReport('/learning'),
                  onError: (_, __) {},
                ),
                AsyncActionButton(
                  label: 'Review quality',
                  busyLabel: 'Reading…',
                  doneLabel: null,
                  busy: reportBusy && s._learningCommand == '/quality',
                  onPressed:
                      reportBusy ? null : () => s._learningReport('/quality'),
                  onError: (_, __) {},
                ),
              ]),
              below: _trackedView(s, 'learning'),
            ),
          ],
        ),
        SettingsSection(
          title: 'Lesson provenance',
          description: 'Where lessons come from.',
          children: [
            if (sources.isEmpty)
              const RuntimeEmptyRow('No lessons yet.',
                  icon: Icons.school_outlined),
            for (final entry in sources)
              ValueRow(
                key: Key('lesson-source-${entry.key}'),
                label: _capitalized(entry.key),
                value: '${entry.value}',
              ),
          ],
        ),
        if (signals.isNotEmpty)
          SettingsSection(
            title: 'Outcome signals',
            description:
                'What the recorded outcomes said, most frequent first.',
            children: [
              for (final signal in signals)
                ValueRow(
                  key: Key('outcome-signal-${signal.signal}'),
                  label: _capitalized(signal.signal.replaceAll('_', ' ')),
                  description: signal.good ? 'Good outcome' : 'Bad outcome',
                  value: '${signal.count} · reward '
                      '${signal.averageReward.toStringAsFixed(2)}',
                ),
            ],
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
    return '${parts.join(', ')}.';
  }
}
