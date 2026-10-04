part of '../runtime_screen.dart';

/// An autopilot run or task status as a vocabulary kind.
StatusKind autopilotStatusKind(String value) {
  if (value == 'completed' || value == 'passed') return StatusKind.ok;
  if (value == 'failed' || value == 'blocked') return StatusKind.fail;
  if (value == 'running' || value == 'planning' || value == 'in_progress') {
    return StatusKind.running;
  }
  if (value == 'cancelled' || value == 'superseded') return StatusKind.skipped;
  if (value == 'paused' || value == 'ready' || value == 'interrupted') {
    return StatusKind.warn;
  }
  return StatusKind.note;
}

String _humanStatus(String value) =>
    value.isEmpty ? 'unknown' : value.replaceAll('_', ' ');

/// Start a goal: what to do, how far it may reach, and Plan only / Run goal.
class _AutopilotComposer extends StatelessWidget {
  final _RuntimeScreenState s;
  const _AutopilotComposer(this.s);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final busy = s._busy('autopilot');
    final action = s._autopilotAction;
    final outcome = _trackedView(s, 'autopilot');
    return SettingsSection(
      title: 'Autopilot',
      description: 'Give it an outcome. It plans a checklist, works one '
          'guarded task at a time, and pauses at a budget or a decision.',
      children: [
        RuntimeCardBody(
          child: TextField(
            key: const Key('autopilot-goal'),
            controller: s._autopilotGoal,
            minLines: 2,
            maxLines: 4,
            onChanged: (_) => s._clearGoalError(),
            decoration: InputDecoration(
              labelText: 'Goal',
              hintText: 'Inspect this project, implement the missing '
                  'feature, and run its tests',
              alignLabelWithHint: true,
              errorText: s._autopilotGoalError,
            ),
          ),
        ),
        SwitchRow(
          switchKey: const Key('autopilot-observe'),
          label: 'Observe only',
          description: s._autopilotObserve
              ? 'Reads the project; nothing is changed.'
              : 'Off: works in the workspace and may edit files, with the '
                  'usual approvals.',
          value: s._autopilotObserve,
          onChanged: s._setAutopilotObserve,
        ),
        SwitchRow(
          label: 'Public web',
          description: 'Tasks may search and read public pages.',
          value: s._autopilotWeb,
          onChanged: s._setAutopilotWeb,
        ),
        SwitchRow(
          label: 'Adaptive review',
          description: 'Re-plans when a task result calls for it.',
          value: s._autopilotAdaptive,
          onChanged: s._setAutopilotAdaptive,
        ),
        RuntimeCardBody(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Wrap(
                spacing: SonderSpace.sm,
                runSpacing: SonderSpace.sm,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  AsyncActionButton(
                    buttonKey: const Key('autopilot-run'),
                    label: 'Run goal',
                    icon: Icons.rocket_launch_outlined,
                    busyLabel: 'Starting…',
                    doneLabel: null,
                    style: ActionButtonStyle.filled,
                    busy: busy && action == 'run',
                    onPressed: busy && action != 'run'
                        ? null
                        : () => s._autopilotRequest('run'),
                    onError: (_, __) {},
                  ),
                  AsyncActionButton(
                    buttonKey: const Key('autopilot-plan'),
                    label: 'Plan only',
                    icon: Icons.account_tree_outlined,
                    busyLabel: 'Planning…',
                    doneLabel: null,
                    busy: busy && action == 'plan',
                    onPressed: busy && action != 'plan'
                        ? null
                        : () => s._autopilotRequest('plan'),
                    onError: (_, __) {},
                  ),
                  AsyncActionButton(
                    label: 'Check status',
                    busyLabel: 'Checking…',
                    doneLabel: null,
                    style: ActionButtonStyle.text,
                    busy: busy && action == 'status',
                    onPressed: busy && action != 'status'
                        ? null
                        : () => s._autopilotRequest('status'),
                    onError: (_, __) {},
                  ),
                ],
              ),
              const SizedBox(height: SonderSpace.md),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Padding(
                  padding: const EdgeInsets.only(top: SonderSpace.xxs),
                  child: Icon(Icons.shield_outlined,
                      size: 16, color: tokens.muted),
                ),
                const SizedBox(width: SonderSpace.sm),
                Expanded(
                  child: Text(
                    'Autopilot never gets location consent, cloud tiers, '
                    'delete, account, permission or fleet controls.',
                    style: text.bodySmall,
                  ),
                ),
              ]),
              if (outcome != null) ...[
                const SizedBox(height: SonderSpace.md),
                outcome,
              ],
            ],
          ),
        ),
      ],
    );
  }
}

/// The latest autonomous run: objective, progress, counts and controls,
/// then its success gates and checklist; events and the end report stay
/// raw, behind disclosures.
class _AutopilotRunCard extends StatelessWidget {
  final _RuntimeScreenState s;
  final AutopilotStatus status;

  const _AutopilotRunCard({required this.s, required this.status});

  @override
  Widget build(BuildContext context) {
    final run = status.latest!;
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final passed = run.tasks.where((task) => task.status == 'passed').length;
    final superseded =
        run.tasks.where((task) => task.status == 'superseded').length;
    final settled = passed + superseded;
    final progress = run.tasks.isEmpty ? 0.0 : settled / run.tasks.length;
    final kind = autopilotStatusKind(run.status);
    final controlBusy = s._busy('autopilot-control');
    final outcome = _trackedView(s, 'autopilot-control');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(
          key: const Key('autopilot-run-card'),
          title: run.isActive || run.isResumable ? 'Current run' : 'Latest run',
          description: [
            if (status.activeRuns > 0) '${status.activeRuns} active',
            if (status.resumableRuns > 0) '${status.resumableRuns} resumable',
            if (status.totalRuns > 0) '${status.totalRuns} in total',
          ].join(' · '),
          trailing: StatusPill(kind, word: _humanStatus(run.status)),
          children: [
            RuntimeCardBody(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(run.objective,
                      style: text.titleSmall
                          ?.copyWith(fontWeight: FontWeight.w600)),
                  const SizedBox(height: SonderSpace.xs),
                  SelectableText(
                    [
                      run.id,
                      _humanStatus(run.phase),
                      run.project.isEmpty ? 'default project' : run.project,
                      run.policy.isEmpty ? null : run.policy,
                      run.tier.isEmpty ? 'local' : 'local ${run.tier}',
                      run.allowWeb ? 'web on' : 'web off',
                      run.adaptive ? 'adaptive' : 'static plan',
                    ].whereType<String>().join(' · '),
                    style: tokens.mono(12, color: tokens.text2),
                  ),
                  const SizedBox(height: SonderSpace.lg),
                  Meter(
                    value: progress,
                    label: 'Tasks settled',
                    valueLabel: '$settled of ${run.tasks.length}',
                    higherIsBetter: true,
                    warnAt: 1.1,
                    dangerAt: 1.1,
                  ),
                ],
              ),
            ),
            RuntimeStatStrip([
              RuntimeStat('Cycles', '${run.cycles}'),
              RuntimeStat('Failures', '${run.failures} of ${run.maxFailures}'),
              RuntimeStat('Checkpoints', '${run.checkpoints}'),
              RuntimeStat('Replans', '${run.replans} of ${run.maxReplans}'),
            ]),
            if (run.summary.isNotEmpty || run.lastError.isNotEmpty)
              RuntimeCardBody(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (run.summary.isNotEmpty)
                      Text(run.summary, style: text.bodyMedium),
                    if (run.lastError.isNotEmpty) ...[
                      if (run.summary.isNotEmpty)
                        const SizedBox(height: SonderSpace.md),
                      WorkspaceNotice(
                        kind: StatusKind.fail,
                        title: run.lastError,
                        framed: false,
                        liveRegion: false,
                      ),
                    ],
                  ],
                ),
              ),
            if (!run.isTerminal || outcome != null)
              RuntimeCardBody(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (!run.isTerminal)
                      Wrap(
                        spacing: SonderSpace.sm,
                        runSpacing: SonderSpace.sm,
                        children: [
                          if (run.isResumable)
                            AsyncActionButton(
                              label: 'Resume',
                              icon: Icons.play_arrow_outlined,
                              busyLabel: 'Resuming…',
                              doneLabel: null,
                              style: ActionButtonStyle.filled,
                              busy: controlBusy,
                              onPressed: () =>
                                  s._controlAutopilot('resume', run),
                              onError: (_, __) {},
                            ),
                          if (run.isActive)
                            AsyncActionButton(
                              label: 'Pause',
                              icon: Icons.pause_outlined,
                              busyLabel: 'Pausing…',
                              doneLabel: null,
                              busy: controlBusy,
                              onPressed: () =>
                                  s._controlAutopilot('pause', run),
                              onError: (_, __) {},
                            ),
                          AsyncActionButton(
                            label: 'Cancel run',
                            busyLabel: 'Cancelling…',
                            doneLabel: null,
                            style: ActionButtonStyle.text,
                            busy: controlBusy,
                            confirm: () => s._confirmCancelAutopilot(run),
                            onPressed: () => s._controlAutopilot('cancel', run),
                            onError: (_, __) {},
                          ),
                        ],
                      ),
                    if (outcome != null) ...[
                      if (!run.isTerminal)
                        const SizedBox(height: SonderSpace.md),
                      outcome,
                    ],
                  ],
                ),
              ),
          ],
        ),
        if (run.criteria.isNotEmpty ||
            run.tasks.isNotEmpty ||
            status.events.isNotEmpty ||
            run.finalReport.isNotEmpty)
          SettingsSection(
            title: 'Plan',
            description: run.tasks.isEmpty
                ? null
                : '$settled of ${run.tasks.length} tasks settled',
            children: [
              if (run.criteria.isNotEmpty)
                RuntimeCardBody(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Success gates', style: text.labelMedium),
                      const SizedBox(height: SonderSpace.sm),
                      for (final criterion in run.criteria)
                        Padding(
                          padding:
                              const EdgeInsets.only(bottom: SonderSpace.xs),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Padding(
                                padding:
                                    const EdgeInsets.only(top: SonderSpace.xxs),
                                child: Icon(Icons.flag_outlined,
                                    size: 16, color: tokens.text2),
                              ),
                              const SizedBox(width: SonderSpace.sm),
                              Expanded(
                                  child:
                                      Text(criterion, style: text.bodyMedium)),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              for (final task in run.tasks)
                RuntimeRow(
                  key: Key('autopilot-task-${task.id}'),
                  kind: autopilotStatusKind(task.status),
                  word: _humanStatus(task.status),
                  title: RuntimeRowTitle(
                      task.title.isEmpty ? task.id : task.title),
                  subtitle: RuntimeRowDetail(
                      [
                        [task.id, task.kind]
                            .where((part) => part.isNotEmpty)
                            .join(' · '),
                        if (task.error.isNotEmpty)
                          task.error
                        else if (task.instruction.isNotEmpty)
                          task.instruction,
                      ].join('\n'),
                      maxLines: 3),
                ),
              if (status.events.isNotEmpty)
                RawDisclosure(
                  title: 'Run events (${status.events.length})',
                  text: status.events
                      .map((event) => '${event.kind}: ${event.message}')
                      .join('\n'),
                ),
              if (run.finalReport.isNotEmpty)
                RawDisclosure(title: 'End report', text: run.finalReport),
            ],
          ),
      ],
    );
  }
}
