part of '../runtime_screen.dart';

/// Memory & learning: learning quality, the raw memory reports (collapsed),
/// safe self-improvement and grounded practice.
class _MemoryPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _MemoryPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    if (info == null) {
      return _PageColumn(children: [
        _NotLoadedSection(title: 'Learning quality', loading: s._loading),
        _PracticeSection(s),
      ]);
    }
    final reports = <(String, String, String)>[
      ('Memory tiers', info.learnTiers, 'memory-tiers'),
      if (info.improvements.trim().isNotEmpty)
        ('Improvements', info.improvements, 'improvements'),
      if (info.stats.trim().isNotEmpty) ('Stats', info.stats, 'stats'),
    ];
    return _PageColumn(children: [
      if (info.learningHealth != null)
        _LearningHealthPanel(s: s, health: info.learningHealth!),
      SettingsSection(
        key: const Key('memory-reports'),
        title: 'Raw reports',
        description: 'The runtime\'s own text reports, as it wrote them.',
        children: [
          for (final (title, text, key) in reports)
            RawDisclosure(
              key: Key('memory-report-$key'),
              title: title,
              text: text.trim().isEmpty ? '(empty)' : text,
            ),
        ],
      ),
      if (info.selfmod != null) _SelfmodPanel(s: s, info: info.selfmod!),
      _PracticeSection(s),
    ]);
  }
}

/// Grounded practice on this PC, and a fixed number of practice cases.
class _PracticeSection extends StatelessWidget {
  final _RuntimeScreenState s;
  const _PracticeSection(this.s);

  @override
  Widget build(BuildContext context) {
    final local = s._localRuntimeControls;
    return SettingsSection(
      key: const Key('practice-section'),
      title: 'Practice',
      description: 'Grounded practice turns real outcomes into lessons.',
      children: [
        SettingRow(
          label: 'Grounded practice',
          description: local
              ? 'Runs the endless grounded-practice loop on this PC until '
                  'you stop it.'
              : 'Runs on the runtime host; start it there.',
          enabled: local,
          trailing: AsyncActionButton(
            label: 'Start practice',
            icon: Icons.all_inclusive,
            busyLabel: 'Starting…',
            doneLabel: null,
            busy: s._busy('practice'),
            onPressed: local
                ? () => s._runLocal('practice', 'Grounded practice',
                    LocalManager.startEndlessTraining)
                : null,
            onError: (_, __) {},
          ),
          below: _trackedView(s, 'practice'),
        ),
        SettingRow(
          label: 'Practice cases',
          description: 'Run a fixed number of grounded cases, 1 to 500.',
          trailing: Wrap(
              spacing: SonderSpace.sm,
              runSpacing: SonderSpace.sm,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                SizedBox(
                  width: 88,
                  child: TextField(
                    key: const Key('practice-cases'),
                    controller: s._trainCount,
                    keyboardType: TextInputType.number,
                    textAlign: TextAlign.end,
                    decoration: const InputDecoration(
                      isDense: true,
                      labelText: 'Cases',
                    ),
                  ),
                ),
                AsyncActionButton(
                  buttonKey: const Key('practice-run'),
                  label: 'Run practice',
                  busyLabel: 'Running…',
                  doneLabel: null,
                  busy: s._busy('train'),
                  onPressed: () => s._trackCommand('train', s._trainCommand()),
                  onError: (_, __) {},
                ),
              ]),
          below: _trackedView(s, 'train'),
        ),
      ],
    );
  }
}
