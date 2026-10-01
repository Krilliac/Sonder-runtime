part of '../runtime_screen.dart';

/// MCP tool convergence: tool implementations and schemas stage in
/// isolation and replace the active registry only after the new source
/// loads cleanly. Status and Refresh run on demand, here.
class _McpRuntimePanel extends StatelessWidget {
  final _RuntimeScreenState s;
  final McpRuntimeInfo runtime;

  const _McpRuntimePanel({required this.s, required this.runtime});

  @override
  Widget build(BuildContext context) {
    final healthy = runtime.status == 'current' && !runtime.hasWarning;
    final warnings = <String>[
      if (runtime.sourceChanged)
        'Newer source is waiting for the next atomic MCP refresh.',
      if (runtime.lastError.isNotEmpty)
        '${runtime.lastError} (last known-good tools remain active)',
      if (runtime.lastNotificationError.isNotEmpty)
        'Tool-list notification: ${runtime.lastNotificationError}',
    ];
    final busy = s._busy('mcp');
    final command = s._commandOf['mcp'];
    return SettingsSection(
      key: const Key('mcp-runtime-panel'),
      title: 'MCP tools',
      description: 'New tool code loads in isolation and replaces the '
          'active tools only once it loads cleanly.',
      trailing: StatusPill(healthy ? StatusKind.ok : StatusKind.warn,
          word: 'MCP ${runtime.status}', dense: true),
      children: [
        RuntimeStatStrip([
          RuntimeStat('Tools', '${runtime.registeredTools}'),
          RuntimeStat('Refreshes', '${runtime.refreshCount}'),
          RuntimeStat('Tool list',
              runtime.protocolListChanged ? 'Live updates' : 'Static'),
        ]),
        if (warnings.isNotEmpty)
          Padding(
            padding: const EdgeInsets.all(SonderSpace.lg),
            child: WorkspaceNotice(
              kind: StatusKind.warn,
              title: warnings.first,
              detail: warnings.length > 1 ? warnings.skip(1).join('\n') : null,
              framed: false,
              liveRegion: false,
            ),
          ),
        ValueRow(
          label: 'Loaded',
          value: runtime.loadedShort.isEmpty ? 'unknown' : runtime.loadedShort,
          mono: true,
        ),
        ValueRow(
          label: 'Current source',
          value:
              runtime.currentShort.isEmpty ? 'unknown' : runtime.currentShort,
          mono: true,
        ),
        if (runtime.path.isNotEmpty)
          ValueRow(
            label: 'Source file',
            value: runtime.path,
            mono: true,
            copyable: true,
          ),
        SettingRow(
          label: 'Check or reload tools',
          description: 'Refresh retries the swap safely; the last good '
              'tools stay active if it fails.',
          trailing: Wrap(spacing: SonderSpace.sm, children: [
            AsyncActionButton(
              label: 'Check status',
              busyLabel: 'Checking…',
              doneLabel: null,
              busy: busy && command == '/mcp status',
              onPressed:
                  busy ? null : () => s._slotCommand('mcp', '/mcp status'),
              onError: (_, __) {},
            ),
            AsyncActionButton(
              label: 'Refresh tools',
              busyLabel: 'Refreshing…',
              doneLabel: null,
              busy: busy && command == '/mcp refresh',
              onPressed:
                  busy ? null : () => s._slotCommand('mcp', '/mcp refresh'),
              onError: (_, __) {},
            ),
          ]),
          below: _trackedView(s, 'mcp'),
        ),
      ],
    );
  }
}
