part of '../runtime_screen.dart';

/// Observatory: the live export, its connect URLs, Open and Copy.
class _ObservatoryPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ObservatoryPage(this.s);

  @override
  Widget build(BuildContext context) {
    final settings = s.widget.settings;
    return _PageColumn(children: [
      EcosystemPanel(
        reading: s._ecosystem,
        error: s._ecosystemError,
        loading: s._loadingExtras,
        runtimeUrl: settings.serverUrl,
        canStartProcesses: LocalManager.canRunLocalTools,
        onLaunch: s._launchObservatory,
        usesCredential: s._usesCredential,
        parts: const {EcosystemPart.observatory},
      ),
      SettingsSection(
        key: const Key('observatory-app'),
        title: 'How this app opens it',
        description: 'Set in Settings › Observatory.',
        children: [
          if (LocalManager.canRunLocalTools)
            ValueRow(
              label: 'Executable',
              value: settings.observatoryExecutable.trim().isEmpty
                  ? 'Found on PATH or by SONDER_OBSERVATORY_BIN'
                  : settings.observatoryExecutable,
              mono: settings.observatoryExecutable.trim().isNotEmpty,
            ),
          ValueRow(
            label: 'Web URL',
            value: settings.observatoryWebUrl.trim().isEmpty
                ? 'Not set'
                : settings.observatoryWebUrl,
            mono: settings.observatoryWebUrl.trim().isNotEmpty,
          ),
        ],
      ),
    ]);
  }
}

/// Updates & extensions: shown only when the server reports either (it is
/// admin-only; a 403 or 404 hides the category).
class _UpdatesPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _UpdatesPage(this.s);

  @override
  Widget build(BuildContext context) {
    final update = s._updateStatus;
    final extensions = s._extensionRegistry;
    return _PageColumn(children: [
      if (update != null) _UpdateSection(status: update),
      if (extensions != null) _ExtensionRegistrySection(status: extensions),
    ]);
  }
}

/// Cluster: the deployment profile, the distributed capability surface and
/// the compute nodes.
class _ClusterPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ClusterPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    final deployment = info?.deployment;
    final capabilities = info?.operationalCapabilities;
    return _PageColumn(children: [
      if (info == null)
        _NotLoadedSection(title: 'Deployment profile', loading: s._loading)
      else if (deployment == null && capabilities == null)
        const SettingsSection(
          title: 'Deployment profile',
          children: [
            RuntimeEmptyRow('Single PC: no deployment profile reported.',
                icon: Icons.computer_outlined),
          ],
        ),
      if (deployment != null)
        _DeploymentPanel(key: const Key('deployment-panel'), info: deployment),
      if (capabilities != null)
        _OperationalCapabilitiesPanel(
          key: const Key('operational-capabilities-panel'),
          info: capabilities,
          showRecoveryRows: deployment == null,
        ),
      ComputeNodesList(source: s._data),
    ]);
  }
}
