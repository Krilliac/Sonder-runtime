part of '../settings_screen.dart';

/// Account: sign in to this server, or manage the session you have. The
/// deployment API key is never touched by any of it.
extension _AccountPage on _SettingsScreenState {
  Widget _accountPage(BuildContext context) {
    final account = _account;
    return account == null
        ? _signInSection(context)
        : _sessionSection(context, account);
  }

  Widget _signInSection(BuildContext context) {
    final publicHosting = _isPublicHosting;
    final accountOnly = _isAccountAuthMode;
    final pwHint = publicHosting && _password.text.isNotEmpty
        ? validatePublicHostingPassword(_password.text,
            username: _username.text)
        : null;
    return SettingsSection(
      title: 'Sign in',
      description: publicHosting || accountOnly
          ? 'Sign in or create an account without a shared host API key.'
          : 'Login keeps your deployment API key.',
      children: [
        SettingsFieldRow(
          label: 'Username',
          field: LabeledTextField(
            fieldKey: const Key('settings-username'),
            label: 'Username',
            controller: _username,
          ),
        ),
        SettingsFieldRow(
          label: 'Password',
          description: publicHosting
              ? 'At least 12 characters with 3+ classes (upper, lower, digit, '
                  'symbol), not containing your username.'
              : 'At least 8 characters. The first account becomes admin.',
          field: LabeledTextField(
            fieldKey: const Key('settings-password'),
            label: 'Password',
            controller: _password,
            obscureText: true,
            onChanged: publicHosting ? (_) => _update(() {}) : null,
          ),
          below: pwHint == null
              ? null
              : FieldNote(
                  StatusKind.warn,
                  pwHint,
                  key: const Key('settings-public-password-hint'),
                ),
        ),
        if (_needsBootstrap && !publicHosting)
          SettingsFieldRow(
            label: 'Bootstrap secret',
            description: 'The SONDER_BOOTSTRAP_SECRET value configured on the server. '
                'Used once, never saved.',
            field: LabeledTextField(
              fieldKey: const Key('settings-bootstrap-secret'),
              label: 'Bootstrap secret',
              controller: _bootstrapSecret,
              obscureText: _obscureBootstrap,
              mono: true,
              suffix: VisibilityToggle(
                obscured: _obscureBootstrap,
                what: 'bootstrap secret',
                onPressed: () =>
                    _update(() => _obscureBootstrap = !_obscureBootstrap),
              ),
            ),
          ),
        _ActionRow(
          buttons: [
            AsyncActionButton(
              buttonKey: const Key('settings-login'),
              label: 'Login',
              busyLabel: 'Signing in…',
              doneLabel: null,
              style: ActionButtonStyle.filled,
              icon: Icons.login,
              // Login and Register share the form, so each waits for the
              // other; the running one keeps its own progress.
              onPressed: _signInBusy ? null : _login,
            ),
            AsyncActionButton(
              buttonKey: const Key('settings-register'),
              label: 'Register',
              busyLabel: 'Registering…',
              doneLabel: null,
              icon: Icons.person_add_alt,
              onPressed: _signInBusy ? null : _register,
            ),
          ],
          outcome: _accountOutcome,
          onDismiss: () => _update(() => _accountOutcome = null),
        ),
      ],
    );
  }

  Widget _sessionSection(BuildContext context, AccountSession account) {
    final here = account.matches(_server.text);
    final whoUser = _whoami?.username.trim() ?? '';
    final user = whoUser.isNotEmpty
        ? whoUser
        : (account.token == _signedInToken ? _signedInUser : null);
    final role = _whoami?.role.trim() ?? '';
    return SettingsSection(
      title: 'Session',
      description: 'Sign out revokes this session on the server. Forget '
          'local session removes it from this device only.',
      children: [
        FactRow(
          label: 'Signed-in server',
          value: account.origin,
          mono: true,
          kind: here ? StatusKind.ok : StatusKind.warn,
        ),
        if (user != null) FactRow(label: 'Signed in as', value: user),
        if (role.isNotEmpty)
          FactRow(
            key: const Key('settings-account-role'),
            label: 'Role',
            value: role,
          ),
        if (_isPublicHosting || _capabilities?.clientExecSupported == true)
          FactRow(
            key: const Key('settings-account-client-exec'),
            label: 'Tool execution',
            value: _clientExecEnabled
                ? 'On this PC (mode: $_clientPermissionMode)'
                : 'Disabled',
          ),
        if (!here)
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, SonderSpace.md, SonderSpace.lg, 0),
            child: FieldNote(
              StatusKind.warn,
              'This session belongs to ${account.origin}, not the server on '
              'the Connection page. Return to it to sign out, or forget the '
              'session here.',
            ),
          ),
        _ActionRow(
          buttons: [
            AsyncActionButton(
              buttonKey: const Key('settings-sign-out'),
              label: 'Sign out',
              busyLabel: 'Signing out…',
              doneLabel: null,
              icon: Icons.logout,
              onPressed: !here || _sessionBusy ? null : _signOut,
            ),
            AsyncActionButton(
              buttonKey: const Key('settings-forget-session'),
              label: 'Forget local session',
              busyLabel: 'Forgetting…',
              doneLabel: null,
              style: ActionButtonStyle.text,
              icon: Icons.phonelink_erase_outlined,
              onPressed: _sessionBusy ? null : _forgetSession,
            ),
          ],
          outcome: _accountOutcome,
          onDismiss: () => _update(() => _accountOutcome = null),
        ),
        _apiAccessSection(context),
        _swarmStatusSection(context),
      ],
    );
  }

  SonderApi get _accountApi => SonderApi(
        baseUrl: _server.text,
        apiKey: _key.text,
        accountSession:
            _account?.matches(_server.text) == true ? _account : null,
      );

  /// Runs one account-API call and shows what the server answered. A failure
  /// is shown as a failure, never as success.
  Future<void> _runAccountCall(
    String title,
    Future<Map<String, dynamic>> Function(SonderApi api) call,
  ) async {
    try {
      final reply = await call(_accountApi);
      if (!mounted) return;
      _update(() => _apiOutcome = ActionOutcome.ok(
            title,
            output: const JsonEncoder.withIndent('  ').convert(reply),
          ));
    } on SonderException catch (e) {
      if (!mounted) return;
      _update(() => _apiOutcome = ActionOutcome.failed(title, detail: e.message));
    }
  }

  Widget _swarmStatusSection(BuildContext context) {
    return SettingsSection(
      title: 'Swarm Status',
      description: 'Active swarm agent monitoring. Needs an admin session.',
      children: [
        AsyncActionButton(
          label: 'Refresh Swarm Status',
          icon: Icons.refresh,
          onPressed: () =>
              _runAccountCall('Swarm status', (api) => api.getSwarmStatus()),
        ),
      ],
    );
  }

  /// Runs one key call and records what the server answered; a refusal is
  /// shown as a failure.
  Future<void> _runKeyCall(
    String title,
    Future<Map<String, dynamic>> Function(SonderApi api) call,
    void Function(Map<String, dynamic> reply) onReply, {
    String? detail,
  }) async {
    try {
      final reply = await call(_accountApi);
      if (!mounted) return;
      _update(() {
        onReply(reply);
        _apiOutcome = ActionOutcome.ok(title, detail: detail);
      });
    } on SonderException catch (e) {
      if (!mounted) return;
      _update(() => _apiOutcome = ActionOutcome.failed(title, detail: e.message));
    }
  }

  Widget _apiAccessSection(BuildContext context) {
    final newKey = _newApiKey;
    return SettingsSection(
      title: 'API Access',
      description: 'Manage your assigned API keys for Sonder API usage. '
          'A key you generate stands for this account, with no admin authority.',
      children: [
        _ActionRow(
          outcome: _apiOutcome,
          onDismiss: () => _update(() => _apiOutcome = null),
          buttons: [
            AsyncActionButton(
              label: 'List Keys',
              icon: Icons.list,
              onPressed: () => _runKeyCall(
                'API keys',
                (api) => api.listApiKeys(),
                (reply) {
                  final keys = reply['keys'];
                  _apiKeyIds = keys is List
                      ? keys
                          .whereType<Map>()
                          .map((k) => '${k['key_id'] ?? ''}')
                          .where((id) => id.isNotEmpty)
                          .toList(growable: false)
                      : const <String>[];
                },
                detail: 'Revoke a key from the list below.',
              ),
            ),
            AsyncActionButton(
              label: 'Generate New Key',
              icon: Icons.vpn_key,
              onPressed: () => _runKeyCall(
                'New API key',
                (api) => api.generateApiKey(),
                (reply) {
                  final key = reply['key'];
                  _newApiKey = key is String && key.isNotEmpty ? key : null;
                  final id = '${reply['key_id'] ?? ''}';
                  if (id.isNotEmpty && !_apiKeyIds.contains(id)) {
                    _apiKeyIds = [..._apiKeyIds, id];
                  }
                },
                detail: 'Copy the key now. It is shown once and cannot be '
                    'shown again.',
              ),
            ),
          ],
        ),
        if (newKey != null)
          Padding(
            key: const Key('settings-new-api-key'),
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, 0, SonderSpace.lg, SonderSpace.md),
            child: Row(
              children: [
                Expanded(child: SelectableText(newKey)),
                IconButton(
                  key: const Key('settings-new-api-key-copy'),
                  tooltip: 'Copy key',
                  icon: const Icon(Icons.copy),
                  onPressed: () =>
                      Clipboard.setData(ClipboardData(text: newKey)),
                ),
                TextButton(
                  key: const Key('settings-new-api-key-hide'),
                  onPressed: () => _update(() => _newApiKey = null),
                  child: const Text('Hide'),
                ),
              ],
            ),
          ),
        if (_apiKeyIds.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, 0, SonderSpace.lg, SonderSpace.md),
            child: Wrap(
              spacing: SonderSpace.sm,
              runSpacing: SonderSpace.sm,
              children: [
                for (final id in _apiKeyIds)
                  AsyncActionButton(
                    buttonKey: Key('settings-revoke-key-$id'),
                    label: 'Revoke $id',
                    icon: Icons.delete_forever,
                    onPressed: () => _runKeyCall(
                      'Revoke API key',
                      (api) => api.revokeApiKey(id),
                      (_) => _apiKeyIds =
                          _apiKeyIds.where((k) => k != id).toList(growable: false),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}
