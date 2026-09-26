import 'dart:async';

import 'account_session.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'local_manager.dart';
import 'runtime/status_word.dart';
import 'settings.dart';
import 'theme.dart';
import 'workspace_ui.dart';

/// What "Test connection" found, as one status word plus one-line remedy
/// (plan P1-3, P0-2).
enum ServerReachability {
  reachable('reachable', StatusKind.ok),
  refused('refused (421)', StatusKind.refused),
  needsHttps('needs HTTPS for sign-in', StatusKind.warn),
  unauthorized('needs a key', StatusKind.warn),
  rateLimited('wait', StatusKind.warn),
  unreachable('unreachable', StatusKind.fail),
  failed('error', StatusKind.fail);

  final String word;
  final StatusKind status;
  const ServerReachability(this.word, this.status);
}

class ConnectionDiagnosis {
  final ServerReachability state;
  final String title;
  final String detail;

  /// The PC-side setting that fixes a 421, e.g. `SONDER_ALLOWED_HOSTS=mypc`.
  final String? serverSetting;

  /// Android emulator hint for `10.0.2.2`.
  final String? adbHint;

  const ConnectionDiagnosis(this.state, this.title,
      {this.detail = '', this.serverSetting, this.adbHint});

  bool get ok => state == ServerReachability.reachable;
}

bool _isLoopback(String host) =>
    host == 'localhost' ||
    host == '::1' ||
    RegExp(r'^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$').hasMatch(host);

int? _statusOf(Object error) {
  if (error is SonderException) {
    if (error.httpStatus != null) return error.httpStatus;
    // Older transport builds only put the status in the message.
    final match = RegExp(r'HTTP (\d{3})').firstMatch(error.message);
    if (match != null) return int.parse(match.group(1)!);
  }
  return null;
}

/// A successful probe of [serverUrl].
ConnectionDiagnosis diagnoseReachable(String serverUrl, {int modelCount = 0}) {
  final uri = Uri.tryParse(serverUrl.trim());
  final host = uri?.host ?? '';
  final models = modelCount == 1 ? '1 model' : '$modelCount models';
  if (uri != null && uri.scheme == 'http' && !_isLoopback(host)) {
    return ConnectionDiagnosis(
      ServerReachability.needsHttps,
      'Reachable at $host ($models), but sign-in needs HTTPS off this device.',
      detail: 'The API key works over this address. For accounts, serve the '
          'PC over HTTPS (Tailscale Serve or a TLS proxy that keeps the Host '
          'header).',
    );
  }
  return ConnectionDiagnosis(
      ServerReachability.reachable, 'Connected to $host. $models available.');
}

/// A failed probe or sign-in, turned into what the person can do next.
ConnectionDiagnosis diagnoseConnectionError(Object error, String serverUrl) {
  final uri = Uri.tryParse(serverUrl.trim());
  final host = uri?.host.isNotEmpty == true ? uri!.host : serverUrl.trim();
  final port = uri?.hasPort == true ? uri!.port : 11435;
  final status = _statusOf(error);
  final code = error is SonderException ? error.code : '';
  if (status == 421 || code == 'HOST_NOT_ALLOWED') {
    final emulator = host == '10.0.2.2';
    return ConnectionDiagnosis(
      ServerReachability.refused,
      'Refused: the server at $host refused this address.',
      detail: "Connect with the PC's IP (or 127.0.0.1 with adb reverse), or "
          'add $host to [server].allowed_hosts / SONDER_ALLOWED_HOSTS on the '
          'PC and restart Sonder.',
      serverSetting: 'SONDER_ALLOWED_HOSTS=$host',
      adbHint: emulator
          ? 'Android emulator: run adb reverse tcp:$port tcp:$port, then use '
              'http://127.0.0.1:$port.'
          : null,
    );
  }
  if (status == 401 || status == 403) {
    return ConnectionDiagnosis(
      ServerReachability.unauthorized,
      'Reached $host, but it needs a valid API key or account.',
      detail: 'Paste the deployment API key from the PC, or sign in below.',
    );
  }
  if (status == 429) {
    final wait = error is SonderException ? error.retryAfterSeconds : null;
    return ConnectionDiagnosis(
      ServerReachability.rateLimited,
      wait == null
          ? 'Too many failed sign-ins from this network. Try again shortly.'
          : 'Too many failed sign-ins from this network. Try again in $wait s.',
    );
  }
  if (status != null) {
    return ConnectionDiagnosis(
        ServerReachability.failed, 'The server at $host answered HTTP $status.',
        detail: error is SonderException ? error.message : '');
  }
  if (error is ArgumentError) {
    return const ConnectionDiagnosis(
      ServerReachability.needsHttps,
      'Sign-in needs HTTPS off this device.',
      detail: 'Use an https:// server URL for accounts, or keep using the '
          'API key over the LAN.',
    );
  }
  return ConnectionDiagnosis(
    ServerReachability.unreachable,
    "Can't reach $host.",
    detail: 'Check that Sonder is running on the PC, that both devices are on '
        'the same network or tailnet, and that the port ($port) is right.',
  );
}

/// The first admin needs the bootstrap secret the server printed.
class BootstrapSecretRequired implements Exception {
  final String message;
  const BootstrapSecretRequired(this.message);
  @override
  String toString() => message;
}

/// Network actions Settings performs, all through lane A's [SonderApi].
/// Tests substitute a fake.
class SettingsConnection {
  const SettingsConnection();

  Future<List<String>> testServer(
          String serverUrl, String apiKey, AccountSession? account) =>
      SonderApi(baseUrl: serverUrl, apiKey: apiKey, accountSession: account)
          .listModels();

  Future<String> login(
          String serverUrl, String apiKey, String username, String password) =>
      SonderApi(baseUrl: serverUrl, apiKey: apiKey).login(username, password);

  Future<void> logout(String apiKey, AccountSession account) => SonderApi(
          baseUrl: account.origin, apiKey: apiKey, accountSession: account)
      .logout();

  /// Returns "Account <u> created (role <r>)." on 200 or 201, through lane
  /// A's [SonderApi.register] (the secret travels only as
  /// `X-Sonder-Bootstrap-Secret`). A first-admin 403 becomes
  /// [BootstrapSecretRequired] so the card can ask for the secret.
  Future<String> register(
    String serverUrl,
    String apiKey,
    String username,
    String password, {
    String? bootstrapSecret,
  }) async {
    try {
      return await SonderApi(baseUrl: serverUrl, apiKey: apiKey).register(
          username, password,
          bootstrapSecret: bootstrapSecret?.trim() ?? '');
    } on SonderException catch (error) {
      if (error.needsBootstrapSecret) {
        throw BootstrapSecretRequired(error.message);
      }
      rethrow;
    }
  }
}

/// Connection settings: server URL, API key, theme, plus a "Test connection"
/// button that hits /v1/models so the user gets immediate feedback.
class SettingsScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onChanged;
  final ValueChanged<WorkspaceDestination>? onNavigate;
  final SettingsConnection connection;

  const SettingsScreen({
    super.key,
    required this.settings,
    required this.onChanged,
    this.onNavigate,
    this.connection = const SettingsConnection(),
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _server;
  late final TextEditingController _key;
  late final TextEditingController _model;
  late final TextEditingController _contextSize;
  late final TextEditingController _username;
  late final TextEditingController _password;
  late final TextEditingController _launcherUrl;
  late final TextEditingController _launcherToken;
  late final TextEditingController _observatoryExecutable;
  late final TextEditingController _observatoryWebUrl;

  /// First-admin bootstrap secret: memory only, never in [Settings], cleared
  /// after each use and when this screen goes away (plan P0-9).
  final TextEditingController _bootstrapSecret = TextEditingController();
  bool _needsBootstrap = false;
  late String _themeMode;
  late bool _allowHosted;
  late bool _keepServerRunning;
  late bool _allowApproximateLocation;
  AccountSession? _account;
  bool _obscureKey = true;
  bool _obscureLauncherToken = true;
  bool _obscureBootstrap = true;
  String? _status;
  bool _statusOk = false;
  ConnectionDiagnosis? _connection;
  String? _keyringWarning;
  bool _testing = false;
  bool _dirty = false;
  late final List<TextEditingController> _trackedControllers;

  @override
  void initState() {
    super.initState();
    _account = widget.settings.accountSession;
    _server = TextEditingController(text: widget.settings.serverUrl);
    _key = TextEditingController(text: widget.settings.apiKey);
    _model = TextEditingController(text: widget.settings.model);
    _contextSize = TextEditingController(text: widget.settings.contextSize);
    _username = TextEditingController();
    _password = TextEditingController();
    _launcherUrl = TextEditingController(text: widget.settings.launcherUrl);
    _launcherToken = TextEditingController(text: widget.settings.launcherToken);
    _observatoryExecutable =
        TextEditingController(text: widget.settings.observatoryExecutable);
    _observatoryWebUrl =
        TextEditingController(text: widget.settings.observatoryWebUrl);
    _themeMode = widget.settings.themeMode;
    _allowHosted = widget.settings.allowHosted;
    _keepServerRunning = widget.settings.keepServerRunning;
    _allowApproximateLocation = widget.settings.allowApproximateLocation;
    _trackedControllers = [
      _server,
      _key,
      _model,
      _contextSize,
      _username,
      _password,
      _launcherUrl,
      _launcherToken,
      _observatoryExecutable,
      _observatoryWebUrl,
    ];
    for (final controller in _trackedControllers) {
      controller.addListener(_markDirty);
    }
    _bootstrapServer = _server.text;
    _server.addListener(_serverEdited);
  }

  /// The server the bootstrap secret was asked for. The secret belongs to
  /// that PC only, so editing the URL forgets it rather than sending it to
  /// whatever host is typed next.
  String _bootstrapServer = '';

  void _serverEdited() {
    if (_server.text == _bootstrapServer) return;
    _bootstrapServer = _server.text;
    if (_needsBootstrap || _bootstrapSecret.text.isNotEmpty) {
      setState(_forgetBootstrapSecret);
    }
  }

  @override
  void dispose() {
    for (final controller in _trackedControllers) {
      controller.removeListener(_markDirty);
    }
    _server.removeListener(_serverEdited);
    _server.dispose();
    _key.dispose();
    _model.dispose();
    _contextSize.dispose();
    _username.dispose();
    _password.dispose();
    _launcherUrl.dispose();
    _launcherToken.dispose();
    _observatoryExecutable.dispose();
    _observatoryWebUrl.dispose();
    _bootstrapSecret.clear();
    _bootstrapSecret.dispose();
    super.dispose();
  }

  void _markDirty() {
    if (mounted) setState(() => _dirty = true);
  }

  Future<bool> _confirmDiscard() async {
    if (!_dirty || !mounted) return true;
    final discard = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Discard unsaved settings?'),
        content: const Text(
          'Changes to connection, privacy, or appearance settings have not '
          'been saved.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Keep editing'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Discard changes'),
          ),
        ],
      ),
    );
    return discard == true;
  }

  void _forgetBootstrapSecret() {
    _bootstrapSecret.clear();
    _needsBootstrap = false;
  }

  Future<void> _leaveSettings() async {
    if (!await _confirmDiscard() || !mounted) return;
    _forgetBootstrapSecret();
    Navigator.of(context).pop();
  }

  Future<void> _navigate(WorkspaceDestination destination) async {
    if (!await _confirmDiscard() || !mounted) return;
    _forgetBootstrapSecret();
    widget.onNavigate?.call(destination);
  }

  void _changeBool(ValueChanged<bool> change, bool value) {
    setState(() {
      change(value);
      _dirty = true;
    });
  }

  Settings _current() => Settings(
        serverUrl: _server.text,
        apiKey: _key.text,
        accountSession:
            _account?.matches(_server.text) == true ? _account : null,
        themeMode: _themeMode,
        allowHosted: _allowHosted,
        contextSize: _contextSize.text.trim().isEmpty
            ? '8192'
            : _contextSize.text.trim(),
        keepServerRunning: _keepServerRunning,
        allowApproximateLocation: _allowApproximateLocation,
        launcherUrl: _launcherUrl.text,
        launcherToken: _launcherToken.text,
        observatoryExecutable: _observatoryExecutable.text.trim(),
        observatoryWebUrl: _observatoryWebUrl.text.trim(),
        model: _model.text.trim().isEmpty
            ? Settings.defaultModel
            : _model.text.trim(),
      );

  /// A phone still pointing at its own loopback has not been connected yet.
  bool get _firstRun {
    final host = Uri.tryParse(_server.text.trim())?.host ?? '';
    return !LocalManager.canRunLocalTools &&
        (host.isEmpty || _isLoopback(host));
  }

  Future<void> _test() async {
    setState(() {
      _testing = true;
      _status = null;
      _connection = null;
    });
    try {
      final models = await widget.connection.testServer(
        _server.text,
        _key.text,
        _account?.matches(_server.text) == true ? _account : null,
      );
      if (!mounted) return;
      setState(() => _connection =
          diagnoseReachable(_server.text, modelCount: models.length));
    } catch (error) {
      if (!mounted) return;
      setState(
          () => _connection = diagnoseConnectionError(error, _server.text));
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _copyServerSetting(String setting) async {
    await Clipboard.setData(ClipboardData(text: setting));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('Copied $setting')),
    );
  }

  Future<void> _testLauncher() async {
    final settings = _current();
    final error = settings.launcherConfigurationError;
    if (!settings.hasHostLauncher || error != null) {
      setState(() {
        _statusOk = false;
        _status = error ?? 'Configure the host launcher URL first.';
      });
      return;
    }
    setState(() {
      _testing = true;
      _status = null;
    });
    try {
      final status = await SonderLauncherApi(
        baseUrl: settings.effectiveLauncherUrl,
        token: settings.launcherToken,
      ).status();
      if (!mounted) return;
      final foreignListener = status.serverState == 'foreign_listener';
      final String message;
      if (foreignListener) {
        message = 'Host launcher is reachable, but another service is using '
            'the configured main-server port.';
      } else if (status.ok) {
        message = 'Host launcher is ready; main server is '
            '${status.serverRunning ? "running" : "stopped"}.';
      } else {
        message = 'Host launcher did not report ready.';
      }
      setState(() {
        _statusOk = status.ok && !foreignListener;
        _status = message;
      });
    } on SonderException catch (e) {
      if (!mounted) return;
      setState(() {
        _statusOk = false;
        _status = e.message;
      });
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _register() async {
    await _accountAction(register: true);
  }

  Future<void> _login() async {
    await _accountAction(register: false);
  }

  Future<void> _forgetApiSession() async {
    setState(() {
      _testing = true;
      _status = null;
    });
    try {
      await Settings.clearAccountSession();
      if (!mounted) return;
      _account = null;
      _password.clear();
      if (!mounted) return;
      widget.onChanged(_current());
      setState(() {
        _statusOk = true;
        _status =
            'Account session forgotten locally. Server revocation was not requested.';
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _statusOk = false;
        _status = 'Could not remove the account session securely.';
      });
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _signOut() async {
    final account = _account;
    if (account == null || !account.matches(_server.text)) return;
    setState(() {
      _testing = true;
      _status = null;
    });
    try {
      await widget.connection.logout(_key.text, account);
      await Settings.clearAccountSession();
      if (!mounted) return;
      _account = null;
      _password.clear();
      widget.onChanged(_current());
      setState(() {
        _statusOk = true;
        _status = 'Signed out. This account session was revoked.';
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _statusOk = false;
        _status =
            'Revocation not confirmed. Retry Sign out, or explicitly Forget local session. The session is retained for retry.';
      });
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _accountAction({required bool register}) async {
    if (_account != null) {
      setState(() {
        _statusOk = false;
        _status =
            'Sign out or explicitly forget the current session before switching accounts.';
      });
      return;
    }
    setState(() {
      _testing = true;
      _status = null;
    });
    try {
      final loginOrigin = serverOrigin(_server.text);
      if (register) {
        final secret = _needsBootstrap ? _bootstrapSecret.text : null;
        String msg;
        try {
          msg = await widget.connection.register(
            _server.text,
            _key.text,
            _username.text,
            _password.text,
            bootstrapSecret: secret,
          );
        } finally {
          // Used once, whatever the outcome.
          if (secret != null) _bootstrapSecret.clear();
        }
        if (!mounted) return;
        setState(() {
          _needsBootstrap = false;
          _statusOk = true;
          _status = msg;
        });
      } else {
        final token = await widget.connection
            .login(_server.text, _key.text, _username.text, _password.text);
        if (!mounted) return;
        setState(() {
          _account = AccountSession(token: token, origin: loginOrigin);
          _password.clear();
          _statusOk = true;
          _status = 'Logged in. Save settings to store the token securely.';
        });
      }
    } on BootstrapSecretRequired {
      if (!mounted) return;
      setState(() {
        _needsBootstrap = true;
        _statusOk = false;
        _status = 'The first administrator account needs the bootstrap '
            'secret that Sonder printed on the PC. Enter it below and '
            'register again. It is used once and never saved.';
      });
    } on SonderException catch (e) {
      if (!mounted) return;
      final diagnosis = diagnoseConnectionError(e, _server.text);
      setState(() {
        _statusOk = false;
        if (diagnosis.state == ServerReachability.refused ||
            diagnosis.state == ServerReachability.rateLimited) {
          _connection = diagnosis;
          _status = diagnosis.title;
        } else {
          _status = e.message;
        }
      });
    } on ArgumentError {
      if (!mounted) return;
      setState(() {
        _statusOk = false;
        _status = 'Sign-in needs an https:// server URL off this device.';
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _statusOk = false;
        _status = 'Account request could not be completed.';
      });
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _save() async {
    if (_account != null && !_account!.matches(_server.text)) {
      setState(() {
        _statusOk = false;
        _status =
            'Return to the account server to sign out, or explicitly forget the local session before switching servers.';
      });
      return;
    }
    final s = _current();
    final launcherError = s.launcherConfigurationError;
    if (launcherError != null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(launcherError)),
      );
      return;
    }
    final observatoryError = s.observatoryConfigurationError;
    if (observatoryError != null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(observatoryError)),
      );
      return;
    }
    // A blank field explicitly replaces a credential that was present when
    // this screen opened. Do not leave an old keychain value usable.
    SettingsSaveResult result;
    try {
      if (widget.settings.apiKey.trim().isNotEmpty && s.apiKey.trim().isEmpty) {
        await Settings.clearApiKey();
      }
      if (widget.settings.launcherToken.trim().isNotEmpty &&
          s.launcherToken.trim().isEmpty) {
        await Settings.clearLauncherToken();
      }
      result = await s.save();
    } catch (_) {
      if (!mounted) return;
      setState(() => _keyringWarning =
          'System keyring unavailable: a removed key could not be deleted, '
              'so nothing was saved. Try again once the keyring works.');
      return;
    }
    if (!mounted) return;
    widget.onChanged(s);
    setState(() {
      _dirty = false;
      _keyringWarning = result.warning;
    });
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
          content: Text(result.warning == null
              ? 'Settings saved'
              : 'Settings saved; keys kept in memory only')),
    );
  }

  InputDecoration _field(String label,
          {String? hint, String? helper, IconData? icon, Widget? suffix}) =>
      InputDecoration(
        labelText: label,
        hintText: hint,
        helperText: helper,
        helperMaxLines: 3,
        prefixIcon: icon == null ? null : Icon(icon),
        suffixIcon: suffix,
        border: const OutlineInputBorder(),
      );

  Widget _eye(
          {required bool obscured,
          required String what,
          required VoidCallback onPressed}) =>
      IconButton(
        tooltip: obscured ? 'Show $what' : 'Hide $what',
        icon: Icon(obscured ? Icons.visibility : Icons.visibility_off),
        onPressed: onPressed,
      );

  /// Where "Open Observatory" on the Runtime page looks (contract 10):
  /// the executable (desktop only) and the web URL used when none is found.
  List<Widget> _observatoryGroup(BuildContext context) {
    final webUrl = _observatoryWebUrl.text.trim();
    final webError = observatoryWebUrlError(webUrl);
    final remote =
        webUrl.isNotEmpty && webError == null && !isLoopbackUrl(webUrl);
    return [
      const _GroupLabel('Observatory'),
      if (LocalManager.canRunLocalTools) ...[
        TextField(
          key: const Key('settings-observatory-executable'),
          controller: _observatoryExecutable,
          autocorrect: false,
          decoration: _field(
            'Observatory executable (optional)',
            hint: '/usr/local/bin/sonder-observatory',
            helper: 'Empty uses $observatoryBinEnv, then '
                '$observatoryExecutableName on PATH.',
            icon: Icons.insights_outlined,
          ),
        ),
        const SizedBox(height: 16),
      ],
      TextField(
        key: const Key('settings-observatory-web-url'),
        controller: _observatoryWebUrl,
        keyboardType: TextInputType.url,
        autocorrect: false,
        decoration: _field(
          'Observatory web URL (optional)',
          helper: remote
              ? 'This Observatory is on another host. It can reach only '
                  'producers on its own loopback, so connect it to a runtime '
                  'on that host.'
              : LocalManager.canRunLocalTools
                  ? 'Opened when no Observatory executable is found. HTTPS off '
                      'this device; a local preview build is on loopback port 4173.'
                  : 'Used to build a link to copy; the browser cannot start '
                      'the Observatory.',
          icon: Icons.open_in_browser_outlined,
        ).copyWith(errorText: webError, errorMaxLines: 3),
      ),
    ];
  }

  Widget _connectCard(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final diagnosis = _connection;
    final refused = diagnosis?.state == ServerReachability.refused;
    final title = _firstRun || refused ? 'Connect to your PC' : 'Server';
    return Container(
      key: const Key('settings-connect-card'),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 14),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          if (_firstRun) ...[
            const SizedBox(height: 6),
            Text(
              'Use the HTTPS address your PC publishes on your tailnet or '
              'through a TLS proxy, for example https://your-host.example. '
              'On an emulator, adb reverse lets you use http://127.0.0.1:11435.',
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
                  ?.copyWith(color: tokens.text2),
            ),
          ],
          const SizedBox(height: 12),
          TextField(
            controller: _server,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: _field(
              'Server URL',
              hint: 'https://your-host.example',
              helper: 'HTTPS is required off-device; HTTP is for loopback '
                  'development only.',
              icon: Icons.dns_outlined,
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 12,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              FilledButton.tonalIcon(
                key: const Key('settings-test-connection'),
                onPressed: _testing ? null : _test,
                icon: _testing
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.wifi_tethering),
                label: Text(_testing ? 'Testing…' : 'Test connection'),
              ),
              if (diagnosis != null)
                RuntimeStatusWord(diagnosis.state.status,
                    word: diagnosis.state.word, width: 200),
            ],
          ),
          if (diagnosis != null) ...[
            const SizedBox(height: 10),
            WorkspaceNotice(
              key: const Key('settings-connection-notice'),
              kind: diagnosis.state.status,
              title: diagnosis.title,
              detail: diagnosis.detail.isEmpty ? null : diagnosis.detail,
              hint: diagnosis.adbHint,
              actions: [
                if (diagnosis.serverSetting != null) ...[
                  SelectableText(diagnosis.serverSetting!,
                      style: tokens.mono(12)),
                  TextButton.icon(
                    onPressed: () =>
                        _copyServerSetting(diagnosis.serverSetting!),
                    icon: const Icon(Icons.copy, size: 16),
                    label: const Text('Copy server setting'),
                  ),
                ],
              ],
            ),
          ],
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final signedIn = _account?.matches(_server.text) == true;
    return Scaffold(
      appBar: AppBar(
        automaticallyImplyLeading: false,
        // One return control (plan P2-11), at the leading edge so its
        // tooltip never collides with the window's own Close tooltip.
        leadingWidth: 104,
        leading: Padding(
          padding: const EdgeInsets.only(left: 8),
          child: Tooltip(
            message: 'Back to chat',
            child: TextButton.icon(
              onPressed: _leaveSettings,
              icon: const Icon(Icons.arrow_back, size: 20),
              label: const Text('Chat'),
            ),
          ),
        ),
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Flexible(
              child: Text('Settings', overflow: TextOverflow.ellipsis),
            ),
            if (_dirty) ...[
              const SizedBox(width: 8),
              Semantics(
                label: 'Unsaved changes',
                child: Icon(
                  Icons.circle,
                  size: 9,
                  color: Theme.of(context).colorScheme.primary,
                ),
              ),
            ],
          ],
        ),
        actions: [
          if (widget.onNavigate != null)
            WorkspaceMenu(
                current: WorkspaceDestination.settings, onSelected: _navigate),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 20),
              children: [
                _Readable(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const _GroupLabel('Connection', first: true),
                      _connectCard(context),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _key,
                        obscureText: _obscureKey,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: _field(
                          'API key (optional)',
                          helper: Settings.memoryOnlyCredentials
                              ? 'Kept in memory only in the browser.'
                              : 'Leave blank if the server has auth disabled',
                          icon: Icons.key_outlined,
                          suffix: _eye(
                            obscured: _obscureKey,
                            what: 'API key',
                            onPressed: () =>
                                setState(() => _obscureKey = !_obscureKey),
                          ),
                        ),
                      ),
                      if (_keyringWarning != null) ...[
                        const SizedBox(height: 10),
                        WorkspaceNotice(
                          key: const Key('settings-keyring-warning'),
                          message: _keyringWarning!,
                          tone: NoticeTone.warning,
                        ),
                      ],
                      const SizedBox(height: 16),
                      TextField(
                        controller: _launcherUrl,
                        keyboardType: TextInputType.url,
                        autocorrect: false,
                        decoration: _field(
                          'Host launcher URL (optional)',
                          hint: 'https://your-host:11436',
                          helper:
                              'Explicit HTTPS control endpoint for remote/mobile Start, Stop, and Restart. Never derived from the server URL.',
                          icon: Icons.power_settings_new_outlined,
                        ),
                      ),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _launcherToken,
                        obscureText: _obscureLauncherToken,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: _field(
                          'Host launcher token',
                          helper:
                              'Separate from the main API key; required for LAN startup control.',
                          icon: Icons.vpn_key_outlined,
                          suffix: _eye(
                            obscured: _obscureLauncherToken,
                            what: 'launcher token',
                            onPressed: () => setState(() =>
                                _obscureLauncherToken = !_obscureLauncherToken),
                          ),
                        ),
                      ),
                      const SizedBox(height: 10),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: OutlinedButton.icon(
                          onPressed: _testing ? null : _testLauncher,
                          icon: const Icon(Icons.power_settings_new_outlined),
                          label: const Text('Test host control'),
                        ),
                      ),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _model,
                        autocorrect: false,
                        decoration: _field(
                          'Default model or route',
                          hint: 'sonder, code, fast...',
                          helper: 'Used for new conversations.',
                          icon: Icons.memory_outlined,
                        ),
                      ),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _contextSize,
                        autocorrect: false,
                        decoration: _field(
                          'Context size',
                          hint: '8192, 32k, 256k, 1m',
                          helper:
                              'Requested conversation capacity; server limits still apply.',
                          icon: Icons.view_week_outlined,
                        ),
                      ),
                      const _GroupLabel('Privacy & autonomy'),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Allow hosted/cloud tiers'),
                        subtitle: const Text(
                          'Opt-in only. Prompts sent to cloud tiers leave this machine.',
                        ),
                        value: _allowHosted,
                        onChanged: (v) =>
                            _changeBool((value) => _allowHosted = value, v),
                      ),
                      // Only a build that runs its own local server can keep
                      // it running; phones and the web never start one.
                      if (LocalManager.canRunLocalTools)
                        SwitchListTile(
                          contentPadding: EdgeInsets.zero,
                          title: const Text(
                              'Keep local server running after app closes'),
                          subtitle: const Text(
                            'Use this for headless/background mode. Turn it off if the app '
                            'should stop its local server on exit.',
                          ),
                          value: _keepServerRunning,
                          onChanged: (v) => _changeBool(
                              (value) => _keepServerRunning = value, v),
                        ),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Allow approximate IP location'),
                        subtitle: const Text(
                          'Off by default. For weather or nearby requests, the app asks '
                          'ipwho.is for an approximate city/region. Raw IP is never sent '
                          'to Sonder Runtime, displayed, or retained.',
                        ),
                        value: _allowApproximateLocation,
                        onChanged: (v) => _changeBool(
                          (value) => _allowApproximateLocation = value,
                          v,
                        ),
                      ),
                      const _GroupLabel('Account'),
                      TextField(
                        controller: _username,
                        autocorrect: false,
                        decoration:
                            _field('Username', icon: Icons.person_outline),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: _password,
                        obscureText: true,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: _field(
                          'Password',
                          helper:
                              'At least 8 characters. First account becomes admin.',
                          icon: Icons.lock_outline,
                        ),
                      ),
                      if (_needsBootstrap) ...[
                        const SizedBox(height: 12),
                        TextField(
                          key: const Key('settings-bootstrap-secret'),
                          controller: _bootstrapSecret,
                          obscureText: _obscureBootstrap,
                          autocorrect: false,
                          enableSuggestions: false,
                          decoration: _field(
                            'Bootstrap secret',
                            helper: 'Printed by Sonder on the PC for the first '
                                'admin. Used once, never saved.',
                            icon: Icons.admin_panel_settings_outlined,
                            suffix: _eye(
                              obscured: _obscureBootstrap,
                              what: 'bootstrap secret',
                              onPressed: () => setState(
                                  () => _obscureBootstrap = !_obscureBootstrap),
                            ),
                          ),
                        ),
                      ],
                      const SizedBox(height: 12),
                      Text(_account == null
                          ? 'No account session. Login preserves your deployment API key.'
                          : 'Signed-in server: ${_account!.origin}'),
                      const SizedBox(height: 6),
                      Text(
                        'Sign out revokes this session on the server. Forget local session '
                        'removes it from this device only; it does not revoke it on the server.',
                        style: Theme.of(context)
                            .textTheme
                            .bodySmall
                            ?.copyWith(color: tokens.text2),
                      ),
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          FilledButton.icon(
                            onPressed: _testing ? null : _login,
                            icon: const Icon(Icons.login),
                            label: const Text('Login'),
                          ),
                          // Registering is for a first account or an admin;
                          // it is hidden while a session is active.
                          if (_account == null)
                            OutlinedButton.icon(
                              onPressed: _testing ? null : _register,
                              icon: const Icon(Icons.person_add_alt),
                              label: const Text('Register'),
                            ),
                          OutlinedButton.icon(
                            onPressed: _testing || !signedIn ? null : _signOut,
                            icon: const Icon(Icons.logout),
                            label: const Text('Sign out'),
                          ),
                          OutlinedButton.icon(
                            onPressed: _testing || _account == null
                                ? null
                                : _forgetApiSession,
                            icon: const Icon(Icons.phonelink_erase_outlined),
                            label: const Text('Forget local session'),
                          ),
                        ],
                      ),
                      if (_status != null) ...[
                        const SizedBox(height: 12),
                        WorkspaceNotice(
                            message: _status!,
                            tone: _statusOk
                                ? NoticeTone.success
                                : NoticeTone.warning),
                      ],
                      const _GroupLabel('Appearance'),
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 8),
                        child: Wrap(
                          alignment: WrapAlignment.spaceBetween,
                          crossAxisAlignment: WrapCrossAlignment.center,
                          spacing: 12,
                          runSpacing: 8,
                          children: [
                            Text(
                              'Theme',
                              style: Theme.of(context).textTheme.bodyMedium,
                            ),
                            SegmentedButton<String>(
                              key: const Key('settings-theme-mode'),
                              showSelectedIcon: false,
                              style: SegmentedButton.styleFrom(
                                textStyle: Theme.of(context)
                                    .textTheme
                                    .labelLarge
                                    ?.copyWith(fontFamily: SonderTheme.sans),
                              ),
                              segments: const [
                                ButtonSegment(
                                    value: 'light', label: Text('Light')),
                                ButtonSegment(
                                    value: 'dark', label: Text('Dark')),
                                ButtonSegment(
                                    value: 'system', label: Text('Auto')),
                              ],
                              selected: {_themeMode},
                              onSelectionChanged: (selection) => setState(() {
                                _themeMode = selection.first;
                                _dirty = true;
                              }),
                            ),
                          ],
                        ),
                      ),
                      ..._observatoryGroup(context),
                    ],
                  ),
                ),
              ],
            ),
          ),
          SafeArea(
            minimum: const EdgeInsets.fromLTRB(20, 8, 20, 12),
            child: _Readable(
              padding: EdgeInsets.zero,
              child: SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  key: const Key('settings-save'),
                  onPressed: _dirty ? _save : null,
                  icon: const Icon(Icons.save_outlined),
                  label: const Text('Save'),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The settings column and the Save bar share one reading width.
class _Readable extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const _Readable(
      {required this.child,
      this.padding = const EdgeInsets.symmetric(horizontal: 20)});

  @override
  Widget build(BuildContext context) => Center(
        child: ConstrainedBox(
          constraints: BoxConstraints(maxWidth: 720 + padding.horizontal),
          child: Padding(padding: padding, child: child),
        ),
      );
}

/// A group's name as an eyebrow over a hairline: the settings read as one
/// column with quiet section breaks, not a stack of cards.
class _GroupLabel extends StatelessWidget {
  final String text;
  final bool first;
  const _GroupLabel(this.text, {this.first = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: EdgeInsets.only(top: first ? 4 : 28, bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(text, style: Theme.of(context).textTheme.labelSmall),
          const SizedBox(height: 8),
          Divider(height: 1, color: tokens.hairline),
        ],
      ),
    );
  }
}
