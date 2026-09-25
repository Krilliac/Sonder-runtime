part of '../runtime_screen.dart';

class _McpRuntimePanel extends StatelessWidget {
  final McpRuntimeInfo runtime;

  const _McpRuntimePanel({required this.runtime});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final healthy = runtime.status == 'current' && !runtime.hasWarning;
    final warnings = <String>[
      if (runtime.sourceChanged)
        'Newer source is waiting for the next atomic MCP refresh.',
      if (runtime.lastError.isNotEmpty)
        '${runtime.lastError} (last known-good tools remain active)',
      if (runtime.lastNotificationError.isNotEmpty)
        'Tool-list notification: ${runtime.lastNotificationError}',
    ];
    return Column(
      key: const Key('mcp-runtime-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(
                healthy
                    ? Icons.sync_lock_outlined
                    : Icons.warning_amber_outlined,
                size: 18,
                color: healthy ? cs.primary : cs.error,
              ),
              label: Text('MCP ${runtime.status}'),
            ),
            Chip(
              avatar: const Icon(Icons.build_outlined, size: 18),
              label: Text('${runtime.registeredTools} tools'),
            ),
            Chip(
              avatar: const Icon(Icons.refresh_outlined, size: 18),
              label: Text('${runtime.refreshCount} atomic refreshes'),
            ),
            Chip(
              avatar: Icon(
                runtime.protocolListChanged
                    ? Icons.notifications_active_outlined
                    : Icons.notifications_off_outlined,
                size: 18,
              ),
              label: Text(
                runtime.protocolListChanged
                    ? 'Live tool-list updates'
                    : 'Static tool list',
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          'Tool implementations and schemas stage in isolation, then replace '
          'the active registry only after the updated source loads cleanly.',
          style: Theme.of(context).textTheme.bodyMedium,
        ),
        const SizedBox(height: 10),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: cs.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(10),
          ),
          child: SelectableText(
            'loaded  ${runtime.loadedShort.isEmpty ? 'unknown' : runtime.loadedShort}\n'
            'current ${runtime.currentShort.isEmpty ? 'unknown' : runtime.currentShort}',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  fontFamily: SonderTheme.mono,
                ),
          ),
        ),
        if (warnings.isNotEmpty) ...[
          const SizedBox(height: 10),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: cs.errorContainer,
              borderRadius: BorderRadius.circular(10),
            ),
            child: SelectableText(
              warnings.join('\n'),
              style: TextStyle(color: cs.onErrorContainer),
            ),
          ),
        ],
        if (runtime.path.isNotEmpty) ...[
          const SizedBox(height: 10),
          SelectableText(
            'Loaded source: ${runtime.path}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
        const SizedBox(height: 6),
        Text(
          'Inspect or retry safely with /mcp status or /mcp refresh.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }
}
