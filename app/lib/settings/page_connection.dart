part of '../settings_screen.dart';

/// Connection: where this app reaches Sonder, the key it sends, and the
/// launcher that starts Sonder on another device.
extension _ConnectionPage on _SettingsScreenState {
  Widget _connectionPage(BuildContext context) {
    final diagnosis = _connection;
    final refused = diagnosis?.state == ServerReachability.refused;
    final account = _account;
    final sessionElsewhere =
        account != null && !account.matches(_server.text);
    final hosts = ({..._saved.cleartextKeyHosts, ..._cleartextKeyHosts}
            .toList())
      ..sort();
    final launcherOutcome = _launcherOutcome;
    final runtimeLink = _runtimeLink('Open Runtime');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(
          key: const Key('settings-connect-card'),
          title: _firstRun || refused ? 'Connect to your PC' : 'Server',
          description: _firstRun
              ? 'Use the HTTPS address your PC publishes on your tailnet or '
                  'through a TLS proxy. On an emulator, adb reverse lets you '
                  'use http://127.0.0.1:11435.'
              : null,
          children: [
            SettingsFieldRow(
              label: 'Server URL',
              description:
                  'HTTPS off this device; plain HTTP only on loopback.',
              modified: _serverChanged,
              field: LabeledTextField(
                fieldKey: const Key('settings-server-url'),
                label: 'Server URL',
                controller: _server,
                hint: 'https://your-host.example',
                mono: true,
                keyboardType: TextInputType.url,
              ),
              action: AsyncActionButton(
                buttonKey: const Key('settings-test-connection'),
                label: 'Test connection',
                busyLabel: 'Testing…',
                doneLabel: null,
                icon: Icons.wifi_tethering,
                onPressed: _testConnection,
              ),
              below: diagnosis == null && !sessionElsewhere
                  ? null
                  : Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        if (sessionElsewhere)
                          FieldNote(
                              StatusKind.warn,
                              'Signed in to ${account.origin}. Sign out there, '
                              'or forget that session, to save another '
                              'server.'),
                        if (sessionElsewhere && diagnosis != null)
                          const SizedBox(height: SonderSpace.md),
                        if (diagnosis != null)
                          _DiagnosisView(
                            diagnosis: diagnosis,
                            onCopy: _copyServerSetting,
                          ),
                      ],
                    ),
            ),
            SettingsFieldRow(
              label: 'API key (optional)',
              description: Settings.memoryOnlyCredentials
                  ? 'Kept in memory only in the browser.'
                  : 'Leave blank if the server has auth disabled.',
              modified: _keyChanged,
              field: LabeledTextField(
                fieldKey: const Key('settings-api-key'),
                label: 'API key (optional)',
                controller: _key,
                obscureText: _obscureKey,
                mono: true,
                suffix: VisibilityToggle(
                  obscured: _obscureKey,
                  what: 'API key',
                  onPressed: () => _update(() => _obscureKey = !_obscureKey),
                ),
              ),
              below: !_cleartextKeyAtRisk && _keyringWarning == null
                  ? null
                  : Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        if (_cleartextKeyAtRisk) _cleartextKeyChoice(context),
                        if (_cleartextKeyAtRisk && _keyringWarning != null)
                          const SizedBox(height: SonderSpace.md),
                        if (_keyringWarning != null)
                          FieldNote(
                            StatusKind.warn,
                            _keyringWarning!,
                            key: const Key('settings-keyring-warning'),
                          ),
                      ],
                    ),
            ),
          ],
        ),
        if (hosts.isNotEmpty)
          SettingsSection(
            title: 'API key over plain HTTP',
            description: 'These hosts get the API key unencrypted. Turn one '
                'off to stop sending it there.',
            children: [
              for (final host in hosts)
                _HostRow(
                  host: host,
                  allowed: _cleartextKeyHosts.contains(host),
                  modified: _cleartextKeyHosts.contains(host) !=
                      _saved.cleartextKeyHosts.contains(host),
                  onChanged: (allowed) => _setHostAllowed(host, allowed),
                ),
            ],
          ),
        SettingsSection(
          title: 'Host launcher',
          description: 'Start, stop and restart Sonder on another device.',
          trailing: runtimeLink,
          children: [
            SettingsFieldRow(
              label: 'Host launcher URL (optional)',
              description:
                  'An HTTPS control endpoint, never derived from the server '
                  'URL.',
              modified: _launcherUrlChanged,
              field: LabeledTextField(
                fieldKey: const Key('settings-launcher-url'),
                label: 'Host launcher URL (optional)',
                controller: _launcherUrl,
                hint: 'https://your-host:11436',
                mono: true,
                keyboardType: TextInputType.url,
              ),
            ),
            SettingsFieldRow(
              label: 'Host launcher token',
              description: 'Separate from the API key; required off this '
                  'device.',
              modified: _launcherTokenChanged,
              field: LabeledTextField(
                fieldKey: const Key('settings-launcher-token'),
                label: 'Host launcher token',
                controller: _launcherToken,
                obscureText: _obscureLauncherToken,
                mono: true,
                suffix: VisibilityToggle(
                  obscured: _obscureLauncherToken,
                  what: 'launcher token',
                  onPressed: () => _update(
                      () => _obscureLauncherToken = !_obscureLauncherToken),
                ),
              ),
            ),
            _ActionRow(
              buttons: [
                AsyncActionButton(
                  buttonKey: const Key('settings-test-launcher'),
                  label: 'Test host control',
                  busyLabel: 'Testing…',
                  doneLabel: null,
                  icon: Icons.power_settings_new_outlined,
                  onPressed: _testLauncher,
                ),
              ],
              outcome: launcherOutcome,
              onDismiss: () => _update(() => _launcherOutcome = null),
            ),
          ],
        ),
      ],
    );
  }

  /// The per-host choice for a plain-HTTP server with a key typed.
  Widget _cleartextKeyChoice(BuildContext context) {
    final hostKey = CleartextKeyPolicy.hostKeyOf(_server.text);
    final allowed = _cleartextKeyHosts.contains(hostKey);
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        FieldNote(
          StatusKind.warn,
          allowed
              ? 'The API key is sent to $hostKey over unencrypted HTTP. '
                  'Anyone on the network path can read and reuse it.'
              : 'The API key is not sent to $hostKey: this address uses '
                  'unencrypted HTTP. Use HTTPS, or allow this host.',
          key: const Key('settings-cleartext-key-warning'),
        ),
        const SizedBox(height: SonderSpace.xs),
        // Its own Material: the card's surface would hide the tile's ink.
        Material(
          type: MaterialType.transparency,
          child: CheckboxListTile(
            key: const Key('settings-cleartext-key-allow'),
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            value: allowed,
            title: Text('Send the API key to $hostKey over unencrypted HTTP',
                style: text.bodyMedium?.copyWith(fontWeight: FontWeight.w500)),
            subtitle: Text(
                'Only on a network you trust. This host and port only.',
                style: text.bodySmall),
            onChanged: hostKey.isEmpty
                ? null
                : (v) => _setHostAllowed(hostKey, v == true),
          ),
        ),
      ],
    );
  }
}

/// What "Test connection" found: the reachability word, what it means, and
/// the PC-side fix when there is one.
class _DiagnosisView extends StatelessWidget {
  final ConnectionDiagnosis diagnosis;
  final ValueChanged<String> onCopy;

  const _DiagnosisView({required this.diagnosis, required this.onCopy});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final setting = diagnosis.serverSetting;
    final hint = diagnosis.adbHint;
    final mark = StatusMark(diagnosis.state.status,
        word: diagnosis.state.word, size: 12.5);
    final body = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(diagnosis.title,
            style: text.bodyMedium?.copyWith(color: tokens.text)),
        if (diagnosis.detail.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.xxs),
            child: Text(diagnosis.detail, style: text.bodySmall),
          ),
        if (hint != null)
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.xs),
            child: Text('${SonderStrings.hintLabel} $hint',
                style: text.bodySmall?.copyWith(color: tokens.muted)),
          ),
        if (setting != null)
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.xs),
            child: Wrap(
              spacing: SonderSpace.sm,
              runSpacing: SonderSpace.xs,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                Text(setting, style: tokens.mono(12.5)),
                TextButton.icon(
                  onPressed: () => onCopy(setting),
                  icon: const Icon(Icons.copy_outlined, size: 16),
                  label: const Text('Copy server setting'),
                ),
              ],
            ),
          ),
      ],
    );
    return Semantics(
      key: const Key('settings-connection-notice'),
      container: true,
      liveRegion: true,
      child: LayoutBuilder(builder: (context, constraints) {
        if (constraints.maxWidth < 420) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [mark, const SizedBox(height: SonderSpace.xs), body],
          );
        }
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(padding: const EdgeInsets.only(top: 2), child: mark),
            const SizedBox(width: SonderSpace.md),
            Expanded(child: body),
          ],
        );
      }),
    );
  }
}

/// One allowed plain-HTTP host: its `host:port` and a switch.
class _HostRow extends StatelessWidget {
  final String host;
  final bool allowed;
  final bool modified;
  final ValueChanged<bool> onChanged;

  const _HostRow({
    required this.host,
    required this.allowed,
    required this.modified,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return MergeSemantics(
      child: HoverSurface(
        onTap: () => onChanged(!allowed),
        borderRadius: BorderRadius.zero,
        child: Container(
          constraints: const BoxConstraints(minHeight: 48),
          padding: const EdgeInsets.symmetric(
              horizontal: SonderSpace.lg, vertical: SonderSpace.xs),
          child: Row(children: [
            Expanded(
              child: Row(children: [
                Flexible(
                  child: Text(host,
                      overflow: TextOverflow.ellipsis,
                      style: tokens.mono(13,
                          color: allowed ? tokens.text : tokens.muted)),
                ),
                if (modified) ...[
                  const SizedBox(width: SonderSpace.sm),
                  const ModifiedDot(),
                ],
              ]),
            ),
            Switch(value: allowed, onChanged: onChanged),
          ]),
        ),
      ),
    );
  }
}

/// A row of buttons that act on the section above, and their result.
class _ActionRow extends StatelessWidget {
  final List<Widget> buttons;
  final ActionOutcome? outcome;
  final VoidCallback onDismiss;

  const _ActionRow({
    required this.buttons,
    required this.outcome,
    required this.onDismiss,
  });

  @override
  Widget build(BuildContext context) {
    final outcome = this.outcome;
    return Padding(
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.lg, SonderSpace.md, SonderSpace.lg, SonderSpace.md),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: SonderSpace.sm,
            runSpacing: SonderSpace.sm,
            children: buttons,
          ),
          SonderReveal(
            visible: outcome != null,
            child: outcome == null
                ? const SizedBox.shrink()
                : Padding(
                    padding: const EdgeInsets.only(top: SonderSpace.md),
                    child: OutcomeView(outcome, onDismiss: onDismiss),
                  ),
          ),
        ],
      ),
    );
  }
}
