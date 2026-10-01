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
    return SettingsSection(
      title: 'Sign in',
      description: 'Login keeps your deployment API key.',
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
          description: 'At least 8 characters. The first account becomes '
              'admin.',
          field: LabeledTextField(
            fieldKey: const Key('settings-password'),
            label: 'Password',
            controller: _password,
            obscureText: true,
          ),
        ),
        if (_needsBootstrap)
          SettingsFieldRow(
            label: 'Bootstrap secret',
            description: 'Printed by Sonder on the PC for the first admin. '
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
      ],
    );
  }
}
