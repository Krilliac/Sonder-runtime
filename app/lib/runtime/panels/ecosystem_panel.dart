part of '../runtime_screen.dart';

/// Opens the Observatory for the given connect URLs (desktop: a process or
/// the OS browser; web: a link to copy).
typedef ObservatoryLauncher = Future<ObservatoryLaunchResult> Function(
    List<String> connectUrls);

/// Human name of a provider id.
String providerLabel(String provider) => switch (canonicalProvider(provider)) {
      sonderInferenceProvider => 'Sonder Inference',
      'ollama' => 'Ollama',
      'openai_compatible' => 'OpenAI-compatible',
      final other => other,
    };

StatusKind _providerKind(ProviderState state) => switch (state) {
      ProviderState.ready => StatusKind.ok,
      ProviderState.degraded => StatusKind.warn,
      ProviderState.unavailable => StatusKind.fail,
      ProviderState.unknown => StatusKind.unknown,
    };

StatusKind _inferenceKind(InferenceState state) => switch (state) {
      InferenceState.ready => StatusKind.ok,
      InferenceState.degraded => StatusKind.warn,
      InferenceState.unavailable => StatusKind.fail,
      InferenceState.unknown => StatusKind.unknown,
      InferenceState.notConfigured => StatusKind.skipped,
    };

/// How to bind a tier to Sonder Inference (contract sections 3.1 and 3.2).
const inferenceEnvHint =
    'Set SONDER_MODEL_BACKEND=sonder-inference (or SONDER_<TIER>_PROVIDER, '
    'for example SONDER_CODE_PROVIDER=sonder-inference) and '
    'SONDER_INFERENCE_BASE_URL=http://127.0.0.1:11437, then restart Sonder '
    'Runtime.';

/// Sonder Inference and Observatory status on the Runtime page
/// (`GET /v1/sonder/ecosystem`), with Open Observatory and Copy connect URLs.
///
/// Every state is spoken as a word as well as a colour: ready, degraded,
/// unavailable, unknown, not configured; export on or off.
class EcosystemPanel extends StatefulWidget {
  final EcosystemReading? reading;
  final Object? error;
  final bool loading;

  /// The runtime URL the app talks to. Launching is disabled unless it is
  /// loopback (contract section 10).
  final String runtimeUrl;

  /// False on the web: there is no process to start, only a link to copy.
  final bool canStartProcesses;
  final ObservatoryLauncher onLaunch;

  const EcosystemPanel({
    super.key,
    required this.reading,
    required this.runtimeUrl,
    required this.onLaunch,
    this.error,
    this.loading = false,
    this.canStartProcesses = true,
  });

  @override
  State<EcosystemPanel> createState() => _EcosystemPanelState();
}

class _EcosystemPanelState extends State<EcosystemPanel> {
  ObservatoryLaunchResult? _launch;
  bool _launching = false;
  String? _copied;

  Future<void> _copy(String text, String what) async {
    await Clipboard.setData(ClipboardData(text: text));
    if (mounted) setState(() => _copied = 'Copied $what.');
  }

  Future<void> _open(List<String> urls) async {
    setState(() {
      _launching = true;
      _launch = null;
      _copied = null;
    });
    ObservatoryLaunchResult result;
    try {
      result = await widget.onLaunch(urls);
    } catch (error) {
      result = ObservatoryLaunchResult(
        ok: false,
        mode: ObservatoryLaunchMode.unavailable,
        message: 'Could not open the Observatory: $error',
      );
    }
    if (!mounted) return;
    setState(() {
      _launching = false;
      _launch = result;
    });
  }

  @override
  Widget build(BuildContext context) {
    final reading = widget.reading;
    final error = widget.error;
    final children = <Widget>[
      if (error != null) ...[_errorNotice(error), const SizedBox(height: 12)],
    ];
    if (reading == null) {
      if (error == null) {
        children.add(widget.loading
            ? const StatusRow(
                key: Key('ecosystem-loading'),
                kind: StatusKind.running,
                label: 'Ecosystem',
                value: 'Reading Sonder Inference and Observatory status…',
              )
            : const Text('No ecosystem status loaded yet.',
                key: Key('ecosystem-empty')));
      }
    } else {
      switch (reading.availability) {
        case EcosystemAvailability.unsupportedRuntime:
          children.add(const WorkspaceNotice(
            key: Key('ecosystem-unsupported'),
            kind: StatusKind.skipped,
            word: 'off',
            title: 'This runtime does not report Sonder Inference or '
                'Observatory status.',
            detail: 'GET /v1/sonder/ecosystem answered 404: the runtime '
                'predates it, or neither live export nor provider status is '
                'available.',
            hint: 'update Sonder Runtime, or turn on '
                'SONDER_OBSERVATORY_EXPORT=1',
            liveRegion: false,
          ));
        case EcosystemAvailability.unsupportedSchema:
          children.add(WorkspaceNotice(
            key: const Key('ecosystem-unsupported-schema'),
            kind: StatusKind.warn,
            title: 'Unsupported ecosystem status format.',
            detail: reading.schema.isEmpty
                ? 'The runtime sent no schema name; this app reads '
                    '$ecosystemSchema.'
                : "The runtime sent '${reading.schema}'; this app reads "
                    '$ecosystemSchema.',
            hint: 'update the app to match the runtime',
            liveRegion: false,
          ));
        case EcosystemAvailability.available:
          children.addAll(_status(context, reading.status!));
      }
    }
    return Semantics(
      container: true,
      label: 'Sonder Inference and Observatory',
      explicitChildNodes: true,
      child: Column(
        key: const Key('ecosystem-panel'),
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: children,
      ),
    );
  }

  Widget _errorNotice(Object error) {
    final status = error is SonderException ? error.httpStatus : null;
    if (status == 401 || status == 403) {
      return const WorkspaceNotice(
        key: Key('ecosystem-admin-required'),
        kind: StatusKind.refused,
        title: SonderApi.adminRequiredMessage,
        detail: 'Sonder Inference and Observatory status is admin-only.',
        hint: 'use the deployment API key in Settings, or local-open mode on '
            'the runtime host',
        liveRegion: false,
      );
    }
    return WorkspaceNotice(
      key: const Key('ecosystem-error'),
      kind: StatusKind.fail,
      title: 'Could not load Sonder Inference and Observatory status.',
      detail: error is SonderException ? error.message : error.toString(),
      liveRegion: false,
    );
  }

  List<Widget> _status(BuildContext context, EcosystemStatus status) {
    return [
      _Section(title: 'Provider bindings', child: _bindings(context, status)),
      const SizedBox(height: 12),
      _Section(title: 'Sonder Inference', child: _inference(context, status)),
      if (status.providers.keys.any((id) => id != sonderInferenceProvider)) ...[
        const SizedBox(height: 12),
        _Section(
          title: 'Other providers',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final entry in status.providers.entries)
                if (entry.key != sonderInferenceProvider)
                  StatusRow(
                    key: Key('ecosystem-provider-${entry.key}'),
                    kind: _providerKind(entry.value.state),
                    word: entry.value.state.name,
                    label: providerLabel(entry.key),
                    value: entry.value.baseUrl == null &&
                            entry.value.detail == null
                        ? 'no status reported'
                        : [
                            if (entry.value.baseUrl != null)
                              entry.value.baseUrl!,
                            if (entry.value.detail != null) entry.value.detail!,
                          ].join(' · '),
                  ),
            ],
          ),
        ),
      ],
      const SizedBox(height: 12),
      _Section(title: 'Observatory', child: _observatory(context, status)),
    ];
  }

  Widget _bindings(BuildContext context, EcosystemStatus status) {
    final tokens = SonderTokens.of(context);
    String name(String? id) => id == null ? 'not reported' : providerLabel(id);
    return Column(
      key: const Key('ecosystem-bindings'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _EcosystemField(
          label: 'Default',
          value: name(status.defaultGenerationProvider),
          indent: false,
        ),
        _EcosystemField(
          label: 'Embedding',
          value: name(status.embeddingProvider),
          indent: false,
        ),
        if (status.tierProviders.isNotEmpty) ...[
          const SizedBox(height: 6),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final entry in status.tierProviders.entries)
                Semantics(
                  label: 'Tier ${entry.key} uses ${providerLabel(entry.value)}',
                  excludeSemantics: true,
                  child: Chip(
                    key: Key('ecosystem-tier-${entry.key}'),
                    label: Text('${entry.key} · ${providerLabel(entry.value)}',
                        style: tokens.mono(12)),
                  ),
                ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _inference(BuildContext context, EcosystemStatus status) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final state = status.inferenceState;
    final entry = status.inference;
    if (state == InferenceState.notConfigured) {
      return Column(
        key: const Key('ecosystem-inference'),
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          StatusRow(
            kind: StatusKind.skipped,
            word: state.word,
            label: 'Sonder Inference',
            value: 'Sonder Inference not configured. Every tier uses '
                '${status.boundProviders.map(providerLabel).join(', ')}.',
          ),
          const SizedBox(height: 4),
          SelectableText(inferenceEnvHint,
              key: const Key('ecosystem-inference-hint'),
              style: tokens.mono(12, color: tokens.text2)),
        ],
      );
    }
    final identity = entry?.identity;
    final fallback = status.inferenceFallback;
    final value = [
      if (entry?.detail != null) entry!.detail!,
      if (entry?.detail == null && entry?.baseUrl != null) entry!.baseUrl!,
      if (entry == null) 'Bound, but the runtime reported no status for it.',
    ].join(' · ');
    return Column(
      key: const Key('ecosystem-inference'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(children: [
          Expanded(
            child: StatusRow(
              key: const Key('ecosystem-inference-state'),
              kind: _inferenceKind(state),
              word: state.word,
              label: 'Sonder Inference',
              value: value,
            ),
          ),
          if (entry?.synthetic == true) ...[
            const SizedBox(width: 8),
            Tooltip(
              message: 'Mock backend: synthetic output, not a quality or '
                  'performance signal.',
              child: Semantics(
                label: 'Synthetic: mock backend output, not a quality or '
                    'performance signal',
                excludeSemantics: true,
                child: Chip(
                  key: const Key('ecosystem-synthetic'),
                  avatar: Icon(Icons.science_outlined,
                      size: 16, color: tokens.warn),
                  label: Text('SYNTHETIC',
                      style: tokens.mono(12,
                          color: tokens.warn, weight: FontWeight.w600)),
                ),
              ),
            ),
          ],
        ]),
        if (entry != null) ...[
          if (entry.version != null || entry.apiVersion != null)
            _EcosystemField(
              label: 'Version',
              value: [
                if (entry.version != null) entry.version!,
                if (entry.apiVersion != null) 'API v${entry.apiVersion}',
              ].join(' · '),
            ),
          if (entry.baseUrl != null)
            _EcosystemField(
              label: 'Base URL',
              value: entry.baseUrl!,
              onCopy: () => _copy(entry.baseUrl!, 'the base URL'),
            ),
          if (entry.models.isNotEmpty)
            _EcosystemField(label: 'Models', value: entry.models.join(', ')),
          _EcosystemField(
            key: const Key('ecosystem-identity'),
            label: 'Identity',
            value: identity == null || identity.summary.isEmpty
                ? 'not measured'
                : identity.summary,
          ),
          if (identity != null)
            for (final (name, digest) in identity.digests)
              _EcosystemField(
                key: Key('ecosystem-digest-$name'),
                label: '$name digest',
                value: InferenceIdentity.shortDigest(digest),
                onCopy: () => _copy(digest, 'the $name digest'),
              ),
          if (entry.checkedAt != null)
            _EcosystemField(
              label: 'Checked',
              value: entry.checkedAt!.toLocal().toString().substring(0, 19),
            ),
        ],
        const SizedBox(height: 6),
        Text(
          fallback == null
              ? 'No fallback: requests fail while Sonder Inference is down.'
              : '${providerLabel(fallback)} fallback: ${providerLabel(fallback)} '
                  'serves only requests that never reached Sonder Inference'
                  '${entry != null && entry.fallbackCount > 0 ? ' (used ${entry.fallbackCount} ${entry.fallbackCount == 1 ? 'time' : 'times'})' : ''}.',
          key: const Key('ecosystem-fallback'),
          style: text.bodyMedium,
        ),
      ],
    );
  }

  Widget _observatory(BuildContext context, EcosystemStatus status) {
    final tokens = SonderTokens.of(context);
    final export = status.observatory;
    final urls = observatoryConnectUrls(export?.connectUrls ?? const []);
    final remote = !isLoopbackUrl(widget.runtimeUrl);
    final launch = _launch;
    String count(int? value) => value == null ? '?' : '$value';
    final stats = export == null
        ? 'The runtime reported no Observatory section.'
        : export.exportEnabled
            ? '${count(export.subscribers)} subscribers · '
                '${count(export.emittedEvents)} emitted · '
                '${count(export.droppedEvents)} dropped · '
                '${count(export.retainedEvents)}/${count(export.bufferCapacity)} '
                'retained'
            : 'Runtime live export is off (SONDER_OBSERVATORY_EXPORT=0).';
    final canOpen = urls.isNotEmpty && !remote && !_launching;
    return Column(
      key: const Key('ecosystem-observatory'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        StatusRow(
          key: const Key('ecosystem-export'),
          kind: export?.exportEnabled == true
              ? ((export!.droppedEvents ?? 0) > 0
                  ? StatusKind.warn
                  : StatusKind.ok)
              : StatusKind.skipped,
          word: export?.exportEnabled == true ? null : 'off',
          label: 'Live export',
          value: stats,
        ),
        if (export != null && export.exportEnabled)
          _EcosystemField(
            label: 'Allowed origins',
            value: export.corsOrigins.isEmpty
                ? 'none: browsers on other origins cannot read the stream'
                : export.corsOrigins.join(', '),
          ),
        for (final warning in export?.warnings ?? const <String>[]) ...[
          const SizedBox(height: 6),
          WorkspaceNotice(
            kind: StatusKind.warn,
            title: warning,
            liveRegion: false,
          ),
        ],
        if (urls.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text('Connect URLs', style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 4),
          SelectableText(urls.join('\n'),
              key: const Key('ecosystem-connect-urls'),
              style: tokens.mono(12, color: tokens.text2)),
        ],
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.icon(
              key: const Key('ecosystem-open-observatory'),
              onPressed: canOpen ? () => _open(urls) : null,
              icon: _launching
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.insights_outlined),
              label: Text(widget.canStartProcesses
                  ? 'Open Observatory'
                  : 'Get Observatory link'),
            ),
            OutlinedButton.icon(
              key: const Key('ecosystem-copy-urls'),
              onPressed: urls.isEmpty
                  ? null
                  : () => _copy(urls.join('\n'), 'the connect URLs'),
              icon: const Icon(Icons.copy, size: 18),
              label: const Text('Copy connect URLs'),
            ),
          ],
        ),
        if (remote) ...[
          const SizedBox(height: 8),
          Text(observatoryRemoteExplanation,
              key: const Key('ecosystem-remote'),
              style: Theme.of(context).textTheme.bodySmall),
        ] else if (urls.isEmpty) ...[
          const SizedBox(height: 8),
          Text('The runtime reported no telemetry URLs to connect to.',
              key: const Key('ecosystem-no-urls'),
              style: Theme.of(context).textTheme.bodySmall),
        ],
        if (launch != null) ...[
          const SizedBox(height: 10),
          WorkspaceNotice(
            key: const Key('ecosystem-launch-result'),
            kind: launch.ok ? StatusKind.ok : StatusKind.warn,
            word: launch.ok ? 'done' : null,
            title: launch.message,
            detail: launch.url.isEmpty ? null : launch.url,
            actions: [
              if (launch.url.isNotEmpty)
                OutlinedButton.icon(
                  key: const Key('ecosystem-copy-link'),
                  onPressed: () => _copy(launch.url, 'the Observatory link'),
                  icon: const Icon(Icons.copy, size: 18),
                  label: const Text('Copy link'),
                ),
            ],
          ),
        ],
        if (_copied != null) ...[
          const SizedBox(height: 6),
          Semantics(
            liveRegion: true,
            child: Text(_copied!,
                key: const Key('ecosystem-copied'),
                style: Theme.of(context).textTheme.bodySmall),
          ),
        ],
      ],
    );
  }
}

/// A label/value line under a status row, with an optional copy button.
class _EcosystemField extends StatelessWidget {
  final String label;
  final String value;
  final VoidCallback? onCopy;

  /// Line the label up with the labels of the status rows above.
  final bool indent;

  const _EcosystemField(
      {super.key,
      required this.label,
      required this.value,
      this.onCopy,
      this.indent = true});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final labelText = Text(label,
        style: text.bodyMedium
            ?.copyWith(color: tokens.text2, fontWeight: FontWeight.w500));
    final valueText = SelectableText(value, style: tokens.mono(12));
    // Every line is at least as tall as its copy button, so rows with and
    // without one keep an even rhythm.
    return Padding(
      padding:
          EdgeInsets.only(left: indent ? StatusRow.markWidth : 0, bottom: 2),
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 40),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Expanded(
              child: LayoutBuilder(builder: (context, constraints) {
                if (constraints.maxWidth < 360) {
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [labelText, valueText],
                  );
                }
                return Row(children: [
                  SizedBox(width: StatusRow.labelWidth, child: labelText),
                  Expanded(child: valueText),
                ]);
              }),
            ),
            if (onCopy != null)
              IconButton(
                tooltip: 'Copy $label',
                onPressed: onCopy,
                visualDensity: VisualDensity.compact,
                icon: const Icon(Icons.copy, size: 16),
              ),
          ],
        ),
      ),
    );
  }
}
