part of '../runtime_screen.dart';

/// An agent's server status as a vocabulary kind; the server's own word is
/// shown beside it.
StatusKind agentStatusKind(String status) => switch (status.toLowerCase()) {
      'running' || 'active' || 'working' || 'planning' => StatusKind.running,
      'queued' || 'pending' || 'waiting' => StatusKind.note,
      'done' || 'completed' || 'succeeded' || 'finished' => StatusKind.ok,
      'failed' || 'error' => StatusKind.fail,
      'interrupted' => StatusKind.warn,
      'cancelled' || 'canceled' || 'cancelling' => StatusKind.skipped,
      _ => StatusKind.unknown,
    };

String _gib(int bytes) =>
    '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(1)} GiB';

/// Agents and fleets: capacity, one designed row per agent (status, id,
/// role, what it is doing, calls and tokens), Retry locally for an
/// interrupted master and Cancel active, each behind its confirmation.
class _AgentStatusPanel extends StatelessWidget {
  final _RuntimeScreenState s;
  final AgentStatus? status;

  const _AgentStatusPanel({required this.s, required this.status});

  @override
  Widget build(BuildContext context) {
    final status = this.status;
    if (status == null) {
      return const SettingsSection(
        title: 'Agents',
        children: [
          RuntimeEmptyRow('This runtime does not report agents.',
              icon: Icons.hub_outlined),
        ],
      );
    }
    final capacity = status.capacity;
    final outcome = _trackedView(s, 'agents-cancel');
    final recent = status.agents.take(6).toList();
    return SettingsSection(
      title: 'Agents',
      description: [
        '${status.activeAgents} running',
        '${status.totalAgents} total',
        if (status.cancelPending > 0) '${status.cancelPending} cancelling',
        if (status.interruptedAgents > 0)
          '${status.interruptedAgents} recoverable',
      ].join(' · '),
      children: [
        RuntimeStatStrip([
          RuntimeStat('Running', '${status.activeAgents}'),
          if (capacity != null) ...[
            RuntimeStat('Worker slots', '${capacity.workerSlots}'),
            RuntimeStat('Queue cap', '${capacity.agentCeiling}'),
            RuntimeStat('Free memory', _gib(capacity.availableMemoryBytes)),
          ],
          RuntimeStat('Tokens',
              '${compactCount(status.tokensIn)} in · ${compactCount(status.tokensOut)} out'),
        ]),
        if (recent.isEmpty)
          const RuntimeEmptyRow('No master or subagent activity yet.',
              icon: Icons.hub_outlined),
        for (final agent in recent) _agentRow(context, agent),
        if (status.events.isNotEmpty)
          RawDisclosure(
            title: 'Recent agent events (${status.events.take(8).length})',
            text: status.events.take(8).join('\n'),
          ),
        SettingRow(
          label: 'Stop everything',
          description: 'Cancels queued work and asks running agents to stop.',
          trailing: AsyncActionButton(
            label: 'Cancel active',
            busyLabel: 'Cancelling…',
            doneLabel: null,
            busy: s._busy('agents-cancel'),
            confirm: s._confirmCancelAgents,
            onPressed: s._cancelAgents,
            onError: (_, __) {},
          ),
          below: outcome == null
              ? null
              : KeyedSubtree(
                  key: const Key('runtime-info-notice'), child: outcome),
        ),
      ],
    );
  }

  Widget _agentRow(BuildContext context, AgentActivity agent) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final retryable = agent.role == 'master' && agent.status == 'interrupted';
    final id = 'agent-retry:${agent.id}';
    final what = [
      if (agent.activity.isNotEmpty) agent.activity,
      if (agent.task.isNotEmpty && agent.task != agent.activity) agent.task,
    ];
    final metrics = '${agent.toolCalls} call${agent.toolCalls == 1 ? '' : 's'}'
        ' · ${compactCount(agent.tokensIn)} → ${compactCount(agent.tokensOut)} tok';
    return RuntimeRow(
      key: Key('agent-row-${agent.id}'),
      kind: agentStatusKind(agent.status),
      word: agent.status.isEmpty ? null : agent.status,
      title: Text.rich(
        TextSpan(children: [
          TextSpan(
              text: agent.id, style: tokens.mono(13, weight: FontWeight.w500)),
          if (agent.role.isNotEmpty)
            TextSpan(
                text: '  ${agent.role}',
                style: text.bodySmall?.copyWith(color: tokens.muted)),
        ]),
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final line in what) RuntimeRowDetail(line, maxLines: 2),
          RuntimeRowDetail(metrics, mono: true, maxLines: 1),
        ],
      ),
      actions: [
        if (retryable)
          AsyncActionButton(
            label: 'Retry locally',
            icon: Icons.replay_outlined,
            busyLabel: 'Retrying…',
            doneLabel: null,
            busy: s._busy(id),
            confirm: () => s._confirmRetryAgent(agent.id),
            onPressed: () => s._retryAgent(agent.id),
            onError: (_, __) {},
          ),
      ],
      below: _trackedView(s, id),
    );
  }
}
