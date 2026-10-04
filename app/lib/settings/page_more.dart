part of '../settings_screen.dart';

/// The app version, from `--dart-define=SONDER_APP_VERSION=…` at build time
/// (there is no package_info dependency). Empty in development builds.
const _appVersion = String.fromEnvironment('SONDER_APP_VERSION');

/// Appearance, Privacy, Desktop, Observatory and About.
extension _MorePages on _SettingsScreenState {
  Widget _appearancePage(BuildContext context) {
    return SettingsSection(children: [
      SettingRow(
        label: 'Theme',
        description: 'Applies right away.',
        trailing: SegmentedButton<String>(
          key: const Key('settings-theme-mode'),
          showSelectedIcon: false,
          segments: const [
            ButtonSegment(
              value: 'light',
              icon: Icon(Icons.light_mode_outlined, size: 16),
              label: Text('Light'),
            ),
            ButtonSegment(
              value: 'dark',
              icon: Icon(Icons.dark_mode_outlined, size: 16),
              label: Text('Dark'),
            ),
            ButtonSegment(
              value: 'system',
              icon: Icon(Icons.contrast, size: 16),
              label: Text('System'),
            ),
          ],
          selected: {_saved.themeMode},
          onSelectionChanged: (selection) => _setTheme(selection.first),
        ),
      ),
    ]);
  }

  Widget _privacyPage(BuildContext context) {
    final keyringDown = _keyringWarning != null;
    return SettingsSection(children: [
      SwitchRow(
        switchKey: const Key('settings-approximate-location'),
        label: 'Allow approximate location',
        description: 'Weather and nearby questions may use your city, looked '
            'up by ipwho.is. Your IP is never sent to Sonder.',
        value: _allowApproximateLocation,
        modified: _approximateLocationChanged,
        onChanged: (v) => _stage(() => _allowApproximateLocation = v),
      ),
      // Only a server this app starts reads it (as SONDER_ALLOW_CLOUD), and
      // only desktop builds start one.
      if (LocalManager.canRunLocalTools)
        SwitchRow(
          switchKey: const Key('settings-allow-hosted'),
          label: 'Allow hosted/cloud tiers',
          description: 'Only for the server this app starts: lets it send '
              'prompts to cloud tiers, off this machine.',
          value: _allowHosted,
          modified: _allowHostedChanged,
          onChanged: (v) => _stage(() => _allowHosted = v),
        ),
      FactRow(
        label: 'Stored keys',
        description: 'API key, launcher token and account session.',
        value: Settings.memoryOnlyCredentials
            ? 'In memory only'
            : keyringDown
                ? 'In memory until saved'
                : 'System keyring',
        kind: keyringDown ? StatusKind.warn : null,
      ),
    ]);
  }

  Widget _desktopPage(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(children: [
          SwitchRow(
            switchKey: const Key('settings-keep-server-running'),
            label: 'Keep local server running after the app closes',
            description: 'For headless use. When off, the app stops its '
                'server on exit.',
            value: _keepServerRunning,
            modified: _keepServerRunningChanged,
            onChanged: (v) => _stage(() => _keepServerRunning = v),
          ),
        ]),
        if (_canNavigate)
          RelatedLink(
            text: 'Start, stop and update the local server on the Runtime '
                'page.',
            action: 'Open Runtime',
            onPressed: () => _navigate(WorkspaceDestination.runtime),
          ),
      ],
    );
  }

  /// Where "Open Observatory" on the Runtime page looks (contract 10): the
  /// executable (desktop only) and the web URL used when none is found.
  Widget _observatoryPage(BuildContext context) {
    final webUrl = _observatoryWebUrl.text.trim();
    final webError = observatoryWebUrlError(webUrl);
    final remote =
        webUrl.isNotEmpty && webError == null && !isLoopbackUrl(webUrl);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(children: [
          if (LocalManager.canRunLocalTools)
            SettingsFieldRow(
              label: 'Observatory executable (optional)',
              description: 'Empty uses $observatoryBinEnv, then '
                  '$observatoryExecutableName on PATH. A macOS .app bundle '
                  'works too.',
              modified: _observatoryExecutableChanged,
              field: LabeledTextField(
                fieldKey: const Key('settings-observatory-executable'),
                label: 'Observatory executable (optional)',
                controller: _observatoryExecutable,
                hint: '/usr/local/bin/sonder-observatory',
                mono: true,
              ),
            ),
          SettingsFieldRow(
            label: 'Observatory web URL (optional)',
            description: remote
                ? 'A hosted Observatory opens in this browser and connects '
                    "to the loopback URLs here. Add its origin to the "
                    "runtime's SONDER_CORS_ORIGINS (and Sonder Inference's "
                    '--cors-origin); browsers may also block a public page '
                    'from reading loopback.'
                : LocalManager.canRunLocalTools
                    ? 'Opened when no executable is found. HTTPS off this '
                        'device; a local preview build is on loopback port '
                        '4173.'
                    : 'Used to build a link to copy; the browser cannot '
                        'start the Observatory.',
            modified: _observatoryWebUrlChanged,
            field: LabeledTextField(
              fieldKey: const Key('settings-observatory-web-url'),
              label: 'Observatory web URL (optional)',
              controller: _observatoryWebUrl,
              hint: 'http://127.0.0.1:4173/',
              mono: true,
              keyboardType: TextInputType.url,
              errorText: webError,
            ),
          ),
        ]),
        if (_canNavigate)
          RelatedLink(
            text: 'Open Observatory is on the Runtime page.',
            action: 'Open Runtime',
            onPressed: () => _navigate(WorkspaceDestination.runtime),
          ),
      ],
    );
  }

  Widget _aboutPage(BuildContext context) {
    final account = _saved.accountSession;
    const build = kReleaseMode
        ? 'release'
        : kProfileMode
            ? 'profile'
            : 'debug';
    final pages = CategoryNavigator.maybeOf(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(children: [
          const FactRow(label: 'App', value: 'Sonder Runtime'),
          FactRow(
            label: 'Version',
            value: _appVersion.isEmpty ? 'Development build' : _appVersion,
            mono: _appVersion.isNotEmpty,
            copyable: _appVersion.isNotEmpty,
          ),
          FactRow(
            label: 'Platform',
            value: '${LocalManager.platformLabel} · $build build',
          ),
        ]),
        SettingsSection(
          title: 'Connected to',
          children: [
            FactRow(
              label: 'Server',
              value: _saved.serverUrl.trim(),
              mono: true,
              copyable: true,
              trailing: pages == null
                  ? null
                  : TextButton(
                      onPressed: () =>
                          pages.select(SettingsCategory.connection),
                      child: const Text('Change'),
                    ),
            ),
            FactRow(
              label: 'Account',
              value: account == null ? 'Not signed in' : account.origin,
              mono: account != null,
            ),
          ],
        ),
      ],
    );
  }
}
