part of '../runtime_screen.dart';

class _AgentStatusPanel extends StatelessWidget {
  final AgentStatus status;
  final ValueChanged<String>? onRetry;

  const _AgentStatusPanel({required this.status, this.onRetry});

  @override
  Widget build(BuildContext context) {
    final recent = status.agents.take(6).toList();
    final capacity = status.capacity;
    final availableGiB = capacity == null
        ? 0.0
        : capacity.availableMemoryBytes / (1024 * 1024 * 1024);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: const Icon(Icons.hub_outlined, size: 18),
              label: Text('${status.activeAgents} active'),
            ),
            Chip(
              avatar: const Icon(Icons.list_alt_outlined, size: 18),
              label: Text('${status.totalAgents} total'),
            ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.dynamic_feed_outlined, size: 18),
                label: Text('${capacity.agentCeiling} queued cap'),
              ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.memory_outlined, size: 18),
                label: Text('${capacity.workerSlots} worker slots'),
              ),
            if (capacity != null)
              Chip(
                avatar: const Icon(Icons.storage_outlined, size: 18),
                label: Text('${availableGiB.toStringAsFixed(1)} GiB free'),
              ),
            if (status.cancelPending > 0)
              Chip(
                avatar:
                    const Icon(Icons.cancel_schedule_send_outlined, size: 18),
                label: Text('${status.cancelPending} cancelling'),
              ),
            if (status.interruptedAgents > 0)
              Chip(
                avatar: const Icon(Icons.restore_outlined, size: 18),
                label: Text('${status.interruptedAgents} recoverable'),
              ),
            Chip(
              avatar: const Icon(Icons.keyboard_double_arrow_down, size: 18),
              label: Text('${status.tokensIn} in'),
            ),
            Chip(
              avatar: const Icon(Icons.keyboard_double_arrow_up, size: 18),
              label: Text('${status.tokensOut} out'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        if (recent.isEmpty)
          const _OutputText('No master or subagent activity yet.')
        else
          ...recent.map((agent) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _OutputCard(
                  text: '${agent.id} [${agent.status}] ${agent.activity}\n'
                      'role=${agent.role} calls=${agent.toolCalls} '
                      'tokens=${agent.tokensIn}/${agent.tokensOut}\n'
                      'task: ${agent.task}',
                  action: agent.role == 'master' &&
                          agent.status == 'interrupted' &&
                          onRetry != null
                      ? TextButton.icon(
                          onPressed: () => onRetry!(agent.id),
                          icon: const Icon(Icons.replay_outlined, size: 18),
                          label: const Text('Retry locally'),
                        )
                      : null,
                ),
              )),
        if (status.events.isNotEmpty) ...[
          const SizedBox(height: 4),
          _OutputCard(text: status.events.take(8).join('\n')),
        ],
      ],
    );
  }
}
