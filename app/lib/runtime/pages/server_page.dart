part of '../runtime_screen.dart';

/// Server: the local server process (Start / Stop / Restart, each with its
/// progress and result under it), the host launcher, this install and the
/// server's state paths.
class _ServerPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ServerPage(this.s);

  @override
  Widget build(BuildContext context) {
    final settings = s.widget.settings;
    final info = s._info;
    final localInfo = s._localInfo;
    final canRunLocal = LocalManager.canRunLocalTools;
    return _PageColumn(children: [
      if (!canRunLocal)
        Padding(
          padding: const EdgeInsets.only(bottom: SonderSpace.lg),
          child: WorkspaceNotice(
            key: const Key('runtime-web-only'),
            kind: StatusKind.note,
            title: 'This client cannot inspect local files or start local '
                'processes.',
            detail: settings.usesHostLauncher
                ? 'Start, Stop and Restart control the configured host.'
                : 'Use the desktop app for local setup, or configure a host '
                    'launcher in Settings.',
          ),
        ),
      _LocalServerSection(s),
      _HostLauncherSection(s),
      if (localInfo != null && canRunLocal) _InstallSection(s, localInfo),
      if (info != null) _ServerStateSection(info),
    ]);
  }
}

class _LocalServerSection extends StatelessWidget {
  final _RuntimeScreenState s;
  const _LocalServerSection(this.s);

  @override
  Widget build(BuildContext context) {
    final settings = s.widget.settings;
    final info = s._info;
    final localServer = localServerRow(
      launcherDetected: s._localInfo?.defaultServerReachable ?? false,
      serverUrl: settings.serverUrl,
      connected: info != null && !s._offline && s._serverError == null,
    );
    final busy = s._busy('server');
    final action = s._serverActionId;
    final blocked = busy || s._hostOperationActive || !s._canControlServer;
    bool running(String id) => busy && action == id;
    final launcher = settings.usesHostLauncher;
    final description = launcher
        ? 'Start, Stop and Restart go through the host launcher. Setup, '
            'Git updates and practice stay on that host.'
        : s._localRuntimeControls
            ? 'The bundled server on this PC.'
            : 'Configure an explicit host launcher URL to control a server '
                'from this device.';
    return SettingsSection(
      key: const Key('server-local'),
      title: 'Local server',
      description: description,
      children: [
        StatusValueRow(
          key: const Key('server-status'),
          label: 'Status',
          kind: localServer.$2 ? StatusKind.ok : StatusKind.skipped,
          word: localServer.$2 ? 'ok' : 'off',
          value: localServer.$1,
        ),
        SettingRow(
          label: 'Server process',
          description: s._waitingForLauncherOperation
              ? 'Waiting for the host to finish.'
              : 'Changes apply to every chat and agent on this server.',
          trailing: Wrap(
            spacing: SonderSpace.sm,
            runSpacing: SonderSpace.sm,
            alignment: WrapAlignment.end,
            children: [
              AsyncActionButton(
                buttonKey: const Key('start-server'),
                label: 'Start server',
                icon: Icons.play_arrow_outlined,
                busyLabel: 'Starting…',
                doneLabel: null,
                style: ActionButtonStyle.filled,
                busy: running('start'),
                onPressed: blocked ? null : () => s._serverLifecycle('start'),
                onError: (_, __) {},
              ),
              AsyncActionButton(
                buttonKey: const Key('stop-server'),
                label: 'Stop',
                icon: Icons.stop_circle_outlined,
                busyLabel: 'Stopping…',
                doneLabel: null,
                busy: running('stop'),
                onPressed: blocked ? null : () => s._serverLifecycle('stop'),
                onError: (_, __) {},
              ),
              if (launcher)
                AsyncActionButton(
                  buttonKey: const Key('restart-server'),
                  label: 'Restart',
                  icon: Icons.restart_alt,
                  busyLabel: 'Restarting…',
                  doneLabel: null,
                  busy: running('restart'),
                  onPressed:
                      blocked ? null : () => s._serverLifecycle('restart'),
                  onError: (_, __) {},
                ),
              if (s._waitingForLauncherOperation)
                TextButton.icon(
                  key: const Key('launcher-stop-waiting'),
                  onPressed: s._stopWaitingForLauncherAction,
                  icon: const Icon(Icons.close, size: 18),
                  label: const Text('Stop waiting'),
                ),
            ],
          ),
          below: _trackedView(
            s,
            'server',
            busyLabel: switch (action) {
              'start' => 'Starting server…',
              'stop' => 'Stopping server…',
              'restart' => 'Restarting server…',
              _ => 'Host operation in progress…',
            },
            busyKey: const Key('runtime-busy'),
            failureKey: const Key('runtime-failure'),
          ),
        ),
      ],
    );
  }
}

class _HostLauncherSection extends StatelessWidget {
  final _RuntimeScreenState s;
  const _HostLauncherSection(this.s);

  @override
  Widget build(BuildContext context) {
    final settings = s.widget.settings;
    final launcher = s._launcherInfo;
    final configured = settings.hasHostLauncher;
    final operation = s._launcherOperation;
    String serverText = 'Unknown';
    StatusKind serverKind = StatusKind.unknown;
    if (launcher?.serverState == 'foreign_listener') {
      serverText = 'Conflict: another service is listening on '
          '${launcher!.serverHost}:${launcher.serverPort}';
      serverKind = StatusKind.warn;
    } else if (launcher?.serverRunning == true) {
      serverText = 'Running on ${launcher!.serverHost}:${launcher.serverPort}';
      serverKind = StatusKind.ok;
    } else if (launcher != null) {
      serverText = 'Stopped';
      serverKind = StatusKind.skipped;
    }
    final errors = [
      if (s._launcherError.isNotEmpty) s._launcherError,
      if (settings.launcherConfigurationError != null)
        settings.launcherConfigurationError!,
    ];
    return SettingsSection(
      key: const Key('server-launcher'),
      title: 'Host launcher',
      description: configured
          ? 'Starts and stops the server on another computer, without a '
              'remote shell.'
          : 'Not configured. Set a launcher URL and token in Settings to '
              'control a server on another computer.',
      children: [
        if (errors.isNotEmpty)
          Padding(
            padding: const EdgeInsets.all(SonderSpace.lg),
            child: WorkspaceNotice(
              kind: StatusKind.fail,
              title: errors.first,
              detail: errors.length > 1 ? errors.skip(1).join('\n') : null,
              framed: false,
              liveRegion: false,
            ),
          ),
        ValueRow(
          label: 'Control endpoint',
          value: settings.effectiveLauncherUrl.isEmpty
              ? 'Not configured'
              : settings.effectiveLauncherUrl,
          mono: settings.effectiveLauncherUrl.isNotEmpty,
          kind: !configured
              ? StatusKind.skipped
              : settings.usesHostLauncher && launcher != null
                  ? StatusKind.ok
                  : StatusKind.warn,
          word: !configured ? 'off' : null,
          copyable: settings.effectiveLauncherUrl.isNotEmpty,
        ),
        if (configured) ...[
          StatusValueRow(
            label: 'Launcher',
            value: launcher?.launcher.isNotEmpty == true
                ? launcher!.launcher
                : 'Not reachable',
            kind: launcher?.ok == true ? StatusKind.ok : StatusKind.fail,
          ),
          StatusValueRow(
            label: 'Main server',
            value: serverText,
            kind: serverKind,
            word: serverKind == StatusKind.skipped ? 'off' : null,
          ),
          if (operation != null)
            StatusValueRow(
              label: s._hostOperationActive
                  ? 'Active operation'
                  : 'Last operation',
              value: operation.action.isEmpty
                  ? operation.phase
                  : '${operation.action}: ${operation.phase}',
              kind: s._hostOperationActive
                  ? StatusKind.running
                  : operation.succeeded
                      ? StatusKind.ok
                      : StatusKind.fail,
            ),
        ],
      ],
    );
  }
}

class _InstallSection extends StatelessWidget {
  final _RuntimeScreenState s;
  final LocalInstallInfo localInfo;

  const _InstallSection(this.s, this.localInfo);

  @override
  Widget build(BuildContext context) {
    final local = s._localRuntimeControls;
    return SettingsSection(
      key: const Key('server-install'),
      title: 'This install',
      description: 'The runtime bundled with this app.',
      children: [
        ValueRow(label: 'Platform', value: localInfo.platform),
        ValueRow(
          label: 'Local system',
          value: localInfo.systemExists ? localInfo.systemDir : 'Not bundled',
          mono: localInfo.systemExists,
          kind: localInfo.systemExists ? null : StatusKind.warn,
        ),
        ValueRow(
          label: 'Shared memory',
          value: localInfo.sharedHome,
          mono: true,
          copyable: true,
        ),
        ValueRow(
          label: 'Runtime payload',
          value: localInfo.engineBundle
              ? 'Sealed offline engine included'
              : 'Host runtimes; downloads may be needed',
        ),
        SettingRow(
          label: 'Set up host runtime',
          description: localInfo.bootstrapScript
              ? 'Installs or repairs Python, Ollama and the models this '
                  'runtime needs.'
              : 'The bootstrap script is not bundled with this install.',
          enabled: local,
          trailing: AsyncActionButton(
            label: 'Set up',
            icon: Icons.auto_fix_high_outlined,
            busyLabel: 'Setting up…',
            doneLabel: null,
            busy: s._busy('setup'),
            onPressed: local
                ? () => s._runLocal(
                      'setup',
                      'Setup host runtime',
                      () => LocalManager.setupEngine(
                        allowHosted: s.widget.settings.allowHosted,
                        contextSize: s.widget.settings.contextSize,
                      ),
                    )
                : null,
            onError: (_, __) {},
          ),
          below:
              _trackedView(s, 'setup', failureKey: const Key('setup-failure')),
        ),
        SettingRow(
          label: 'Update from Git',
          description: localInfo.gitCheckout
              ? 'Pulls the latest runtime into the bundled folder.'
              : 'The first update replaces the bundled folder from Git.',
          enabled: local,
          trailing: AsyncActionButton(
            label: 'Update',
            icon: Icons.system_update_alt,
            busyLabel: 'Updating…',
            doneLabel: null,
            busy: s._busy('git-update'),
            onPressed: local
                ? () => s._runLocal(
                    'git-update', 'Update from Git', LocalManager.updateFromGit)
                : null,
            onError: (_, __) {},
          ),
          below: _trackedView(s, 'git-update',
              failureKey: const Key('git-update-failure')),
        ),
      ],
    );
  }
}

class _ServerStateSection extends StatelessWidget {
  final SystemInfo info;
  const _ServerStateSection(this.info);

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      key: const Key('server-state'),
      title: 'Server state',
      description: 'Where the server keeps its database and files.',
      children: [
        if (info.dbPath.isNotEmpty)
          ValueRow(
              label: 'Database',
              value: info.dbPath,
              mono: true,
              copyable: true),
        if (info.stateHome.isNotEmpty)
          ValueRow(
              label: 'Home', value: info.stateHome, mono: true, copyable: true),
        if (info.dbPath.isEmpty && info.stateHome.isEmpty)
          const RuntimeEmptyRow('The server did not report its state paths.',
              icon: Icons.folder_off_outlined),
      ],
    );
  }
}
