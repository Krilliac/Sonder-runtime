part of '../runtime_screen.dart';

class _OperationalCapabilitiesPanel extends StatelessWidget {
  final OperationalCapabilitiesInfo info;
  final SonderApi api;

  const _OperationalCapabilitiesPanel(
      {super.key, required this.info, required this.api});

  String _capabilityValue(OperationalCapabilityInfo capability) {
    if (capability.available) {
      return capability.reason.isEmpty
          ? 'Available'
          : 'Available — ${capability.reason}';
    }
    return capability.reason.isEmpty
        ? 'Unavailable'
        : 'Unavailable — ${capability.reason}';
  }

  String _automaticAvailabilityValue(bool available, String action) {
    return available
        ? 'Available'
        : 'Unavailable — automatic $action is not available.';
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const Key('operational-capabilities-panel-content'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (info.localNode.isNotEmpty)
          _StatusRow(label: 'Compute node', value: info.localNode, ok: true),
        _StatusRow(
          label: 'Compute peers',
          value: '${info.configuredPeerCount}',
          ok: true,
        ),
        _StatusRow(
          label: 'Managed app work',
          value: _capabilityValue(info.managedAppWork),
          ok: info.managedAppWork.available,
        ),
        _StatusRow(
          label: 'Inference pool',
          value:
              '${info.workerSummary}; ${_capabilityValue(info.requestLevelPooling)}',
          ok: info.requestLevelPooling.available,
        ),
        if (info.poolSchemaVersion == 2)
          _StatusRow(
              label: 'Cached capacity',
              value: info.poolCapacitySummary,
              ok: true),
        OllamaPoolDetails(api: api),
        _StatusRow(
          label: 'Whole-job placement',
          value: _capabilityValue(info.wholeJobPlacement),
          ok: info.wholeJobPlacement.available,
        ),
        _StatusRow(
          label: 'Model sharding',
          value: _capabilityValue(info.modelSharding),
          ok: info.modelSharding.available,
        ),
        _StatusRow(
          label: 'Memory replication',
          value: _capabilityValue(info.memoryReplicationTransport),
          ok: info.memoryReplicationTransport.available,
        ),
        _StatusRow(
          label: 'Automatic takeover',
          value: _automaticAvailabilityValue(
            info.automaticTakeoverAvailable,
            'takeover',
          ),
          ok: info.automaticTakeoverAvailable,
        ),
        _StatusRow(
          label: 'Automatic failback',
          value: _automaticAvailabilityValue(
            info.automaticFailbackAvailable,
            'failback',
          ),
          ok: info.automaticFailbackAvailable,
        ),
        _StatusRow(
          label: 'Artifact transfer',
          value: _capabilityValue(info.artifactTransferTransport),
          ok: info.artifactTransferTransport.available,
        ),
        _StatusRow(
          label: 'Automatic memory migration',
          value: _capabilityValue(info.automaticMemoryMigration),
          ok: info.automaticMemoryMigration.available,
        ),
        _StatusRow(
          label: 'Automatic artifact migration',
          value: _capabilityValue(info.automaticArtifactMigration),
          ok: info.automaticArtifactMigration.available,
        ),
        _StatusRow(
          label: 'Indefinite scale',
          value: _capabilityValue(info.indefiniteScale),
          ok: info.indefiniteScale.available,
        ),
      ],
    );
  }
}

/// Administrative details are fetched only by an explicit operator action.
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
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Wrap(spacing: 8, children: [
        OutlinedButton(
            onPressed: _busy ? null : () => _load(),
            child: const Text('Inspect worker page')),
        OutlinedButton(
            onPressed: _busy ? null : () => _load(refresh: true),
            child: const Text('Refresh worker cache')),
        if (page != null && !page.complete && page.nextCursor.isNotEmpty)
          OutlinedButton(
              onPressed: _busy ? null : () => _load(cursor: page.nextCursor),
              child: const Text('Next worker page')),
      ]),
      if (_busy)
        const LinearProgressIndicator(semanticsLabel: 'Loading worker page'),
      if (_error != null) Text(_error!, semanticsLabel: _error),
      if (page != null) ...[
        Text(
            '${page.workers.length} of ${page.workerCount} workers on this page; ${page.omittedWorkerCount} remaining'),
        for (final worker in page.workers)
          SelectableText(
              '${worker.origin}: ${worker.state}; ${worker.modelCount} models; ${worker.errorCategory}\n${worker.modelPreview.join(', ')}'),
      ],
    ]);
  }
}
