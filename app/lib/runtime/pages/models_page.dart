part of '../runtime_screen.dart';

/// Models: the routes the runtime offers and who serves them, the local
/// runtime policy, context health, providers, the inference pool and model
/// fanout.
class _ModelsPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ModelsPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    final routing = ModelRouting.of(s._ecosystem?.status,
        origins: s._modelCatalog?.origins ?? const {});
    return _PageColumn(children: [
      if (info == null)
        _NotLoadedSection(title: 'Routes', loading: s._loading)
      else
        _RoutesSection(models: info.models, routing: routing),
      if (info?.runtimePolicy != null)
        _RuntimePolicyPanel(policy: info!.runtimePolicy!),
      if (info?.context != null) _ContextHealthPanel(health: info!.context!),
      EcosystemPanel(
        reading: s._ecosystem,
        error: s._ecosystemError,
        loading: s._loadingExtras,
        runtimeUrl: s.widget.settings.serverUrl,
        canStartProcesses: LocalManager.canRunLocalTools,
        onLaunch: s._launchObservatory,
        usesCredential: s._usesCredential,
        parts: const {EcosystemPart.inference},
      ),
      if (info?.operationalCapabilities != null)
        _InferencePoolSection(
            info: info!.operationalCapabilities!, api: s._api),
      FanoutList(source: s._data),
    ]);
  }
}

/// One row per route or model the runtime offers, with who serves it.
class _RoutesSection extends StatelessWidget {
  final List<SystemModel> models;
  final ModelRouting routing;

  const _RoutesSection({required this.models, required this.routing});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return SettingsSection(
      key: const Key('models-routes'),
      title: 'Routes',
      description: routing.modelsPanelText(offered: models.map((m) => m.id)),
      children: [
        if (models.isEmpty)
          const RuntimeEmptyRow('The runtime reported no models.',
              icon: Icons.memory_outlined),
        for (final model in models)
          () {
            final route = routing.isRoute(model.id);
            final bound = routing.routeBinding(model.id) != null;
            final served = routing.routeServedModel(model.id);
            final icon = bound
                ? Icons.hub_outlined
                : model.ownedBy == 'cloud'
                    ? Icons.cloud_outlined
                    : Icons.memory_outlined;
            return RuntimeRow(
              key: Key('model-row-${model.id}'),
              markWidth: SonderSpace.x3,
              // A text-line-high box, so the icon sits on the id's line.
              leading: SizedBox(
                height: SonderSpace.xl,
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Icon(icon, size: 18, color: tokens.text2),
                ),
              ),
              title: RuntimeRowTitle(model.id, mono: true, maxLines: 1),
              subtitle: RuntimeRowDetail([
                route ? 'route' : 'exact model',
                if (served != null) 'served with $served',
              ].join(' · ')),
              actions: [
                Text(routing.servedBy(model.id, model.ownedBy),
                    style: text.bodyMedium?.copyWith(color: tokens.text2)),
              ],
            );
          }(),
      ],
    );
  }
}

/// The request-level inference pool: worker health from the cached status,
/// and worker pages only on the explicit Inspect action (UX-CONTRACT.md).
class _InferencePoolSection extends StatelessWidget {
  final OperationalCapabilitiesInfo info;
  final SonderApi api;

  const _InferencePoolSection({required this.info, required this.api});

  @override
  Widget build(BuildContext context) {
    final pooling = info.requestLevelPooling;
    return SettingsSection(
      key: const Key('inference-pool'),
      title: 'Inference pool',
      description: info.workerSummary,
      children: [
        CapabilityRow(
          label: 'Request pooling',
          available: pooling.available,
          reason: pooling.reason,
        ),
        if (info.poolSchemaVersion == 2)
          ValueRow(
            label: 'Cached capacity',
            value: info.poolCapacitySummary,
          ),
        OllamaPoolDetails(api: api),
      ],
    );
  }
}
