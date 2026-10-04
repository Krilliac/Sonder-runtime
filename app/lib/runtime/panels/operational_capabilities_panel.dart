part of '../runtime_screen.dart';

/// The distributed capability surface: what this runtime can place, move
/// and replicate across PCs, each with the runtime's own reason. Unavailable
/// reads `– off` (off by design), never red. The inference pool lives on
/// Models.
class _OperationalCapabilitiesPanel extends StatelessWidget {
  final OperationalCapabilitiesInfo info;

  /// False when the Deployment panel already shows takeover and failback, so
  /// the page never lists the same capability twice (plan P2-10).
  final bool showRecoveryRows;

  const _OperationalCapabilitiesPanel(
      {super.key, required this.info, this.showRecoveryRows = true});

  @override
  Widget build(BuildContext context) {
    CapabilityRow row(String label, OperationalCapabilityInfo capability) =>
        CapabilityRow(
            label: label,
            available: capability.available,
            reason: capability.reason);
    return SettingsSection(
      title: 'Distributed capabilities',
      description:
          'What this runtime can place, move and replicate across PCs.',
      children: [
        if (info.localNode.isNotEmpty)
          ValueRow(label: 'Compute node', value: info.localNode, mono: true),
        ValueRow(label: 'Compute peers', value: '${info.configuredPeerCount}'),
        row('Managed app work', info.managedAppWork),
        row('Whole-job placement', info.wholeJobPlacement),
        row('Model sharding', info.modelSharding),
        row('Memory replication', info.memoryReplicationTransport),
        if (showRecoveryRows) ...[
          CapabilityRow(
            label: 'Automatic takeover',
            available: info.automaticTakeoverAvailable,
            reason: info.automaticTakeoverAvailable
                ? ''
                : 'Automatic takeover is not available.',
          ),
          CapabilityRow(
            label: 'Automatic failback',
            available: info.automaticFailbackAvailable,
            reason: info.automaticFailbackAvailable
                ? ''
                : 'Automatic failback is not available.',
          ),
        ],
        row('Artifact transfer', info.artifactTransferTransport),
        row('Automatic memory migration', info.automaticMemoryMigration),
        row('Automatic artifact migration', info.automaticArtifactMigration),
        row('Indefinite scale', info.indefiniteScale),
      ],
    );
  }
}

StatusKind _workerKind(String state) => switch (state.toLowerCase()) {
      'ready' || 'healthy' || 'available' => StatusKind.ok,
      'degraded' || 'stale' || 'busy' => StatusKind.warn,
      'error' || 'unreachable' || 'failed' || 'unhealthy' => StatusKind.fail,
      _ => StatusKind.unknown,
    };

/// Administrative worker details, fetched only by an explicit operator
/// action: **Inspect worker page** reads one bounded page of the cached
/// pool, **Refresh worker cache** asks for one configured batch, and **Next
/// worker page** follows the server's cursor. Nothing here is polled.
class OllamaPoolDetails extends StatefulWidget {
  final SonderApi api;
  const OllamaPoolDetails({super.key, required this.api});

  @override
  State<OllamaPoolDetails> createState() => _OllamaPoolDetailsState();
}

class _OllamaPoolDetailsState extends State<OllamaPoolDetails> {
  OllamaPoolPage? _page;
  bool _busy = false;
  String? _error;
  int _generation = 0;

  @override
  void didUpdateWidget(covariant OllamaPoolDetails oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.api.baseUrl != widget.api.baseUrl ||
        oldWidget.api.apiKey != widget.api.apiKey ||
        oldWidget.api.accountSession != widget.api.accountSession) {
      _generation++;
      _page = null;
      _error = null;
      _busy = false;
    }
  }

  Future<void> _load({bool refresh = false, String cursor = ''}) async {
    if (_busy) return;
    final generation = ++_generation;
    setState(() {
      _busy = true;
      _error = null;
      _page = null;
    });
    try {
      final page = await widget.api
          .ollamaPoolAdminStatus(refresh: refresh, cursor: cursor);
      if (!mounted || generation != _generation) return;
      setState(() {
        _page = page;
        _busy = false;
      });
    } catch (_) {
      if (!mounted || generation != _generation) return;
      setState(() {
        _busy = false;
        _error =
            'Worker details unavailable. Check administrator access, then inspect again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final page = _page;
    final hasNext =
        page != null && !page.complete && page.nextCursor.isNotEmpty;
    return Column(
      key: const Key('pool-worker-details'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.lg, SonderSpace.md, SonderSpace.lg, SonderSpace.md),
          child: Wrap(
            spacing: SonderSpace.sm,
            runSpacing: SonderSpace.sm,
            children: [
              OutlinedButton(
                  onPressed: _busy ? null : () => _load(),
                  child: const Text('Inspect worker page')),
              OutlinedButton(
                  onPressed: _busy ? null : () => _load(refresh: true),
                  child: const Text('Refresh worker cache')),
              if (hasNext)
                OutlinedButton(
                    onPressed:
                        _busy ? null : () => _load(cursor: page.nextCursor),
                    child: const Text('Next worker page')),
            ],
          ),
        ),
        if (_busy)
          const SkeletonRows(rows: 2, semanticLabel: 'Loading worker page'),
        if (_error != null)
          RuntimePanelNote(status: StatusKind.fail, text: _error!),
        if (page != null) ...[
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, 0, SonderSpace.lg, SonderSpace.xs),
            child: Text(
                '${page.workers.length} of ${page.workerCount} workers on this '
                'page; ${page.omittedWorkerCount} remaining',
                style: Theme.of(context).textTheme.bodySmall),
          ),
          for (final worker in page.workers)
            RuntimeRow(
              dense: true,
              kind: _workerKind(worker.state),
              word: worker.state.isEmpty ? null : worker.state,
              title: RuntimeRowTitle(worker.origin, mono: true, maxLines: 1),
              subtitle: RuntimeRowDetail([
                '${worker.modelCount} model${worker.modelCount == 1 ? '' : 's'}',
                if (worker.errorCategory != 'none') worker.errorCategory,
                if (worker.modelPreview.isNotEmpty)
                  worker.modelPreview.join(', '),
              ].join(' · ')),
            ),
          const SizedBox(height: SonderSpace.sm),
        ],
      ],
    );
  }
}
