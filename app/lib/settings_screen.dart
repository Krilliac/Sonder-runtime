import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'account_session.dart';
import 'api.dart';
import 'local_manager.dart';
import 'runtime/model_routing.dart';
import 'settings.dart';
import 'settings/connection.dart';
import 'settings/context_size.dart';
import 'settings/model_picker.dart';
import 'settings/widgets.dart';
import 'theme.dart';
import 'ui/kit.dart';
import 'ui/status_row.dart';
import 'ui/strings.dart';
import 'workspace_ui.dart';

export 'settings/connection.dart';

part 'settings/page_account.dart';
part 'settings/page_connection.dart';
part 'settings/page_general.dart';
part 'settings/page_more.dart';

/// Stable ids of the Settings pages: deep links ([SettingsScreen]'s
/// `initialCategory`) and the kit's `category-<id>` / `category-page-<id>`
/// keys.
abstract final class SettingsCategory {
  static const general = 'general';
  static const connection = 'connection';
  static const account = 'account';
  static const appearance = 'appearance';
  static const privacy = 'privacy';

  /// Native desktop builds only: the server the app itself starts.
  static const desktop = 'desktop';
  static const observatory = 'observatory';
  static const about = 'about';
}

/// Settings: one calm page per category beside a category rail (a list,
/// then a page, on phones).
///
/// Connection, account, model and privacy values are staged and written
/// together by Save, which a sticky bar offers only while something is
/// unsaved; leaving with unsaved changes asks first. The theme applies and
/// persists at once. Every network action has its own busy state and shows
/// its result under the control that ran it.
class SettingsScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onChanged;
  final ValueChanged<WorkspaceDestination>? onNavigate;
  final SettingsConnection connection;

  /// The page to open first, e.g. [SettingsCategory.account] for the
  /// `/login` intercept. Null opens General on a wide window and the list of
  /// pages on a phone (Connection on a phone that has never connected).
  final String? initialCategory;

  /// Prefills the sign-in user name (the `/login` intercept's argument).
  /// Never a password.
  final String? initialUsername;

  /// Lets an app shell run the unsaved-changes guard before it switches to
  /// another destination. Called with the guard when the screen mounts and
  /// with null when it goes away; store it (do not call setState from this
  /// callback). The guard resolves true when leaving is fine: nothing was
  /// unsaved, or the person chose to discard it.
  final void Function(Future<bool> Function()? guard)? registerLeaveGuard;

  const SettingsScreen({
    super.key,
    required this.settings,
    required this.onChanged,
    this.onNavigate,
    this.connection = const SettingsConnection(),
    this.initialCategory,
    this.initialUsername,
    this.registerLeaveGuard,
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  /// Reading width of every page: the app's conversation width.
  static const _contentWidth = 760.0;

  /// What is persisted and what the app runs with. Staged edits compare
  /// against it; Save, the theme and a fresh sign-in move it.
  late Settings _saved;

  late final TextEditingController _server;
  late final TextEditingController _key;
  late final TextEditingController _model;
  late final TextEditingController _contextSize;
  late final TextEditingController _launcherUrl;
  late final TextEditingController _launcherToken;
  late final TextEditingController _observatoryExecutable;
  late final TextEditingController _observatoryWebUrl;
  late final List<TextEditingController> _stagedText;

  /// Sign-in inputs: not settings, so never persisted and never "unsaved".
  late final TextEditingController _username;
  final TextEditingController _password = TextEditingController();

  /// First-admin bootstrap secret: memory only, never in [Settings], cleared
  /// after each use, when the server URL changes and when this screen goes
  /// away (plan P0-9).
  final TextEditingController _bootstrapSecret = TextEditingController();
  bool _needsBootstrap = false;

  /// The server the bootstrap secret was asked for. The secret belongs to
  /// that PC only, so editing the URL forgets it rather than sending it to
  /// whatever host is typed next.
  String _bootstrapServer = '';

  late bool _allowHosted;
  late bool _keepServerRunning;
  late bool _allowApproximateLocation;

  /// Plain-HTTP hosts allowed to receive the API key (see
  /// [CleartextKeyPolicy]); changed only by an explicit per-host choice.
  late Set<String> _cleartextKeyHosts;
  AccountSession? _account;

  bool _obscureKey = true;
  bool _obscureLauncherToken = true;
  bool _obscureBootstrap = true;

  ConnectionDiagnosis? _connection;
  ActionOutcome? _launcherOutcome;
  ActionOutcome? _accountOutcome;
  String? _keyringWarning;
  String? _saveError;

  /// Why the exact context entry was adjusted, until the next edit.
  String? _contextNote;
  bool _contextEdited = false;
  final FocusNode _contextFocus = FocusNode();

  /// Login and Register share one form, Sign out and Forget one session:
  /// each waits for its sibling. Nothing else on the page is locked.
  bool _signInBusy = false;
  bool _sessionBusy = false;

  /// Who signed in during this run, for that exact session only. The
  /// stored session holds no user name (token and origin are its whole
  /// record), so a restored session shows its server alone.
  String? _signedInUser;
  String? _signedInToken;

  /// The last `/v1/models` answer, for the model picker.
  ModelChoices? _models;
  String? _modelsServer;

  late final _StagedKeyPolicy _keyPolicy =
      _StagedKeyPolicy(() => _cleartextKeyHosts);
  late String? _initialCategory;

  /// The shell's last section request this screen applied, and a counter
  /// that rebuilds the category scaffold at that section.
  ShellSection? _appliedSection;
  int _sectionEpoch = 0;
  bool _leaving = false;

  @override
  void initState() {
    super.initState();
    final s = widget.settings;
    _saved = s.copyWith();
    _account = s.accountSession;
    _server = TextEditingController(text: s.serverUrl);
    _key = TextEditingController(text: s.apiKey);
    _model = TextEditingController(text: s.model);
    _contextSize = TextEditingController(text: s.contextSize);
    _launcherUrl = TextEditingController(text: s.launcherUrl);
    _launcherToken = TextEditingController(text: s.launcherToken);
    _observatoryExecutable =
        TextEditingController(text: s.observatoryExecutable);
    _observatoryWebUrl = TextEditingController(text: s.observatoryWebUrl);
    _username = TextEditingController(text: widget.initialUsername ?? '');
    _allowHosted = s.allowHosted;
    _keepServerRunning = s.keepServerRunning;
    _allowApproximateLocation = s.allowApproximateLocation;
    _cleartextKeyHosts = {...s.cleartextKeyHosts};
    _stagedText = [
      _server,
      _key,
      _model,
      _contextSize,
      _launcherUrl,
      _launcherToken,
      _observatoryExecutable,
      _observatoryWebUrl,
    ];
    for (final controller in _stagedText) {
      controller.addListener(_stagedEdited);
    }
    _bootstrapServer = _server.text;
    _server.addListener(_serverEdited);
    _contextFocus.addListener(_contextFocusChanged);
    _initialCategory = widget.initialCategory ??
        (_firstRun ? SettingsCategory.connection : null);
    widget.registerLeaveGuard?.call(_leaveGuard);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // The shell asks for a section (the connection footer, a /login
    // intercept) with a new ShellSection each time; apply each one once.
    final section = ShellScope.maybeOf(context)?.section;
    if (section == null || identical(section, _appliedSection)) return;
    _appliedSection = section;
    _initialCategory = section.id;
    _sectionEpoch++;
    final username = section.params['username'];
    if (username != null && username.isNotEmpty && _username.text.isEmpty) {
      _username.text = username;
    }
  }

  @override
  void didUpdateWidget(covariant SettingsScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Only a new object is news: a parent rebuilding with the same one must
    // not wind the baseline back past what this screen just saved.
    if (!identical(oldWidget.settings, widget.settings)) {
      _rebase(widget.settings);
    }
  }

  @override
  void dispose() {
    widget.registerLeaveGuard?.call(null);
    for (final controller in _stagedText) {
      controller.removeListener(_stagedEdited);
    }
    _server.removeListener(_serverEdited);
    _contextFocus.removeListener(_contextFocusChanged);
    _contextFocus.dispose();
    for (final controller in _stagedText) {
      controller.dispose();
    }
    _username.dispose();
    _password.dispose();
    _bootstrapSecret.clear();
    _bootstrapSecret.dispose();
    super.dispose();
  }

  // -- Staged values ---------------------------------------------------------

  void _stagedEdited() {
    if (mounted) setState(() => _saveError = null);
  }

  void _serverEdited() {
    if (_server.text == _bootstrapServer) return;
    _bootstrapServer = _server.text;
    setState(() {
      // A result for another address would mislead.
      _connection = null;
      if (_needsBootstrap || _bootstrapSecret.text.isNotEmpty) {
        _forgetBootstrapSecret();
      }
    });
  }

  void _forgetBootstrapSecret() {
    _bootstrapSecret.clear();
    _needsBootstrap = false;
  }

  static void _setText(TextEditingController controller, String text) {
    if (controller.text == text) return;
    controller.value = TextEditingValue(
        text: text, selection: TextSelection.collapsed(offset: text.length));
  }

  static bool _sameSession(AccountSession? a, AccountSession? b) =>
      a?.token == b?.token && a?.origin == b?.origin;

  /// Settings changed elsewhere while this screen stayed open (Chat's model
  /// menu, a shell handing back what Settings saved): the baseline follows,
  /// and so does any field still showing its old saved value. Edits made
  /// here are kept.
  void _rebase(Settings next) {
    final before = _saved;
    void follow(
        TextEditingController controller, String Function(Settings) of) {
      final was = of(before);
      final now = of(next);
      if (was != now && controller.text.trim() == was.trim()) {
        _setText(controller, now);
      }
    }

    follow(_server, (s) => s.serverUrl);
    follow(_key, (s) => s.apiKey);
    follow(_model, (s) => s.model);
    follow(_contextSize, (s) => s.contextSize);
    follow(_launcherUrl, (s) => s.launcherUrl);
    follow(_launcherToken, (s) => s.launcherToken);
    follow(_observatoryExecutable, (s) => s.observatoryExecutable);
    follow(_observatoryWebUrl, (s) => s.observatoryWebUrl);
    if (_allowHosted == before.allowHosted) _allowHosted = next.allowHosted;
    if (_keepServerRunning == before.keepServerRunning) {
      _keepServerRunning = next.keepServerRunning;
    }
    if (_allowApproximateLocation == before.allowApproximateLocation) {
      _allowApproximateLocation = next.allowApproximateLocation;
    }
    if (setEquals(_cleartextKeyHosts, before.cleartextKeyHosts.toSet())) {
      _cleartextKeyHosts = {...next.cleartextKeyHosts};
    }
    if (_sameSession(_account, before.accountSession)) {
      _account = next.accountSession;
    }
    _saved = next.copyWith();
  }

  String get _modelValue {
    final model = _model.text.trim();
    return model.isEmpty ? Settings.defaultModel : model;
  }

  int? get _contextTokens => parseContextTokens(_contextSize.text.trim().isEmpty
      ? contextSizeDefault
      : _contextSize.text);

  bool _textChanged(TextEditingController controller, String saved) =>
      controller.text.trim() != saved.trim();

  bool get _serverChanged => _textChanged(_server, _saved.serverUrl);
  bool get _keyChanged => _textChanged(_key, _saved.apiKey);
  bool get _modelChanged => _modelValue != _saved.model.trim();
  bool get _contextChanged {
    final staged = _contextTokens;
    final saved = parseContextTokens(_saved.contextSize);
    if (staged == null || saved == null) {
      return _textChanged(_contextSize, _saved.contextSize);
    }
    return staged != saved;
  }

  bool get _launcherUrlChanged =>
      _textChanged(_launcherUrl, _saved.launcherUrl);
  bool get _launcherTokenChanged =>
      _textChanged(_launcherToken, _saved.launcherToken);
  bool get _hostsChanged =>
      !setEquals(_cleartextKeyHosts, _saved.cleartextKeyHosts.toSet());
  bool get _accountStaged => !_sameSession(_account, _saved.accountSession);
  bool get _approximateLocationChanged =>
      _allowApproximateLocation != _saved.allowApproximateLocation;
  bool get _allowHostedChanged => _allowHosted != _saved.allowHosted;
  bool get _keepServerRunningChanged =>
      _keepServerRunning != _saved.keepServerRunning;
  bool get _observatoryExecutableChanged =>
      _textChanged(_observatoryExecutable, _saved.observatoryExecutable);
  bool get _observatoryWebUrlChanged =>
      _textChanged(_observatoryWebUrl, _saved.observatoryWebUrl);

  /// Which pages hold staged changes.
  Map<String, bool> get _changes => {
        SettingsCategory.general: _modelChanged || _contextChanged,
        SettingsCategory.connection: _serverChanged ||
            _keyChanged ||
            _hostsChanged ||
            _launcherUrlChanged ||
            _launcherTokenChanged,
        SettingsCategory.account: _accountStaged,
        SettingsCategory.privacy:
            _approximateLocationChanged || _allowHostedChanged,
        SettingsCategory.desktop: _keepServerRunningChanged,
        SettingsCategory.observatory:
            _observatoryExecutableChanged || _observatoryWebUrlChanged,
      };

  static const _pageNames = {
    SettingsCategory.general: 'General',
    SettingsCategory.connection: 'Connection',
    SettingsCategory.account: 'Account',
    SettingsCategory.privacy: 'Privacy',
    SettingsCategory.desktop: 'Desktop',
    SettingsCategory.observatory: 'Observatory',
  };

  List<String> get _changedPages => [
        for (final entry in _changes.entries)
          if (entry.value) _pageNames[entry.key]!,
      ];

  bool get _dirty => !_leaving && _changes.values.any((changed) => changed);

  /// A phone still pointing at its own loopback has not been connected yet.
  bool get _firstRun {
    final host = Uri.tryParse(_server.text.trim())?.host ?? '';
    return !LocalManager.canRunLocalTools &&
        (host.isEmpty || isLoopbackServerHost(host));
  }

  /// Plain HTTP to another device with a key typed: the key would cross the
  /// network unencrypted, so it is withheld unless this host is allowed.
  bool get _cleartextKeyAtRisk =>
      _key.text.trim().isNotEmpty &&
      CleartextKeyPolicy.isCleartextRemote(_server.text);

  /// The staged values as [Settings]. The theme is never staged.
  Settings _current() => Settings(
        serverUrl: _server.text.trim(),
        apiKey: _key.text,
        accountSession:
            _account?.matches(_server.text) == true ? _account : null,
        themeMode: _saved.themeMode,
        allowHosted: _allowHosted,
        contextSize: canonicalContextSize(_contextSize.text),
        keepServerRunning: _keepServerRunning,
        allowApproximateLocation: _allowApproximateLocation,
        launcherUrl: _launcherUrl.text.trim(),
        launcherToken: _launcherToken.text,
        observatoryExecutable: _observatoryExecutable.text.trim(),
        observatoryWebUrl: _observatoryWebUrl.text.trim(),
        cleartextKeyHosts: _cleartextKeyHosts.toList()..sort(),
        model: _modelValue,
      );

  /// For page code (extensions may not call the protected [setState]).
  void _update(VoidCallback change) => setState(change);

  /// A staged edit: it also clears a refused Save's reason.
  void _stage(VoidCallback change) {
    setState(() {
      change();
      _saveError = null;
    });
  }

  void _setHostAllowed(String host, bool allowed) => _stage(() {
        if (allowed) {
          _cleartextKeyHosts.add(host);
        } else {
          _cleartextKeyHosts.remove(host);
        }
      });

  void _contextEditedByHand() {
    _contextEdited = true;
    if (_contextNote != null) setState(() => _contextNote = null);
  }

  void _contextFocusChanged() {
    if (!_contextFocus.hasFocus) _normalizeContextSize();
  }

  /// Leaving the exact entry clamps it into range, and says so.
  void _normalizeContextSize() {
    if (!_contextEdited || !mounted) return;
    final text = _contextSize.text.trim();
    final tokens = parseContextTokens(text);
    if (tokens == null) return;
    final clamped = clampContextTokens(tokens);
    _contextEdited = false;
    _setText(_contextSize, '$clamped');
    setState(() => _contextNote = clamped == tokens
        ? null
        : clamped == contextSizeMin
            ? 'Raised to $contextSizeMin, the smallest useful window.'
            : 'Lowered to ${contextSizeLabel(clamped)}, the most the '
                'runtime accepts.');
  }

  void _setContextPreset(int tokens) {
    _contextEdited = false;
    _setText(_contextSize, '$tokens');
    setState(() => _contextNote = null);
  }

  void _discard() {
    _setText(_server, _saved.serverUrl);
    _setText(_key, _saved.apiKey);
    _setText(_model, _saved.model);
    _setText(_contextSize, _saved.contextSize);
    _setText(_launcherUrl, _saved.launcherUrl);
    _setText(_launcherToken, _saved.launcherToken);
    _setText(_observatoryExecutable, _saved.observatoryExecutable);
    _setText(_observatoryWebUrl, _saved.observatoryWebUrl);
    setState(() {
      _allowHosted = _saved.allowHosted;
      _keepServerRunning = _saved.keepServerRunning;
      _allowApproximateLocation = _saved.allowApproximateLocation;
      _cleartextKeyHosts = {..._saved.cleartextKeyHosts};
      _account = _saved.accountSession;
      _saveError = null;
      _contextNote = null;
      _contextEdited = false;
      _launcherOutcome = null;
    });
  }

  // -- Leaving ---------------------------------------------------------------

  Future<bool> _confirmDiscard() async {
    if (!_dirty || !mounted) return true;
    final pages = _changedPages;
    final where = pages.length <= 1
        ? pages.join()
        : '${pages.sublist(0, pages.length - 1).join(', ')} and ${pages.last}';
    final discard = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Discard unsaved settings?'),
        content: Text(where.isEmpty
            ? 'Your changes have not been saved.'
            : 'Changes in $where have not been saved.'),
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

  /// The guard an app shell runs before switching away. A discard really
  /// discards, since a shell may keep this screen alive.
  Future<bool> _leaveGuard() async {
    if (!mounted) return true;
    if (!await _confirmDiscard()) return false;
    if (mounted) {
      _forgetBootstrapSecret();
      _discard();
    }
    return true;
  }

  Future<void> _leaveSettings() async {
    if (!await _confirmDiscard() || !mounted) return;
    _forgetBootstrapSecret();
    setState(() => _leaving = true);
    final popped = await Navigator.of(context).maybePop();
    if (!popped && mounted) {
      // Nowhere to go back to: the person still chose to discard.
      setState(() => _leaving = false);
      _discard();
    }
  }

  bool get _canNavigate =>
      ShellScope.maybeOf(context) != null || widget.onNavigate != null;

  Future<void> _navigate(WorkspaceDestination destination) async {
    if (!await _confirmDiscard() || !mounted) return;
    _forgetBootstrapSecret();
    final shell = ShellScope.maybeOf(context);
    if (shell != null) {
      // Already confirmed: the shell's own guard must not ask again.
      _discard();
      shell.navigate(destination);
    } else {
      widget.onNavigate?.call(destination);
    }
  }

  // -- Network actions -------------------------------------------------------

  void _rememberModels(
      String server, ModelCatalog catalog, ModelRouting routing) {
    _models = ModelChoices(catalog.ids, routing);
    _modelsServer = server.trim();
  }

  /// `GET /v1/models` and the routing document for the staged server, with
  /// this screen's staged plain-HTTP choice for that host.
  Future<(ModelCatalog, ModelRouting)> _probe(String server, String key) {
    final account = _account?.matches(server) == true ? _account : null;
    return _keyPolicy.run(server, () async {
      final catalog = await widget.connection.testServer(server, key, account);
      final status =
          await widget.connection.routingStatus(server, key, account);
      return (catalog, ModelRouting.of(status, origins: catalog.origins));
    });
  }

  Future<void> _testConnection() async {
    final server = _server.text;
    setState(() => _connection = null);
    ConnectionDiagnosis diagnosis;
    try {
      final (catalog, routing) = await _probe(server, _key.text);
      if (mounted) _rememberModels(server, catalog, routing);
      diagnosis = diagnoseReachable(server,
          modelCount: catalog.ids.length,
          routing: routing,
          models: catalog.ids);
    } catch (error) {
      diagnosis = diagnoseConnectionError(error, server);
    }
    // A result for an address that has since been edited would mislead.
    if (!mounted || _server.text != server) return;
    setState(() => _connection = diagnosis);
  }

  Future<ModelChoices> _loadModelChoices() async {
    final server = _server.text;
    final (catalog, routing) = await _probe(server, _key.text);
    if (mounted && _server.text == server) {
      _rememberModels(server, catalog, routing);
    }
    return ModelChoices(catalog.ids, routing);
  }

  Future<void> _pickModel() async {
    final cached = _modelsServer == _server.text.trim() ? _models : null;
    final picked = await showModelPicker(context,
        current: _modelValue, cached: cached, load: _loadModelChoices);
    if (picked == null || !mounted) return;
    _setText(_model, picked);
  }

  Future<void> _testLauncher() async {
    final settings = _current();
    final error = settings.launcherConfigurationError;
    if (!settings.hasHostLauncher || error != null) {
      setState(() => _launcherOutcome = ActionOutcome(
          StatusKind.warn, error ?? 'Enter the host launcher URL first.'));
      return;
    }
    setState(() => _launcherOutcome = null);
    ActionOutcome outcome;
    try {
      final status = await widget.connection.launcherStatus(
          settings.effectiveLauncherUrl, settings.launcherToken);
      if (status.serverState == 'foreign_listener') {
        outcome = const ActionOutcome(
            StatusKind.warn,
            'The launcher answered, but another service holds '
            'the main server port.');
      } else if (status.ok) {
        outcome = ActionOutcome.ok('The launcher is ready; the main server is '
            '${status.serverRunning ? 'running' : 'stopped'}.');
      } else {
        outcome = const ActionOutcome(
            StatusKind.warn, 'The launcher did not report ready.');
      }
    } on SonderException catch (e) {
      outcome = ActionOutcome.failed(e.message);
    } catch (_) {
      outcome = const ActionOutcome.failed('The launcher check failed.');
    }
    if (mounted) setState(() => _launcherOutcome = outcome);
  }

  Future<void> _login() => _accountAction(register: false);

  Future<void> _register() => _accountAction(register: true);

  Future<void> _accountAction({required bool register}) async {
    if (_account != null) {
      setState(() => _accountOutcome = const ActionOutcome(
          StatusKind.warn,
          'Sign out or explicitly forget the current session before '
          'switching accounts.'));
      return;
    }
    final server = _server.text;
    final key = _key.text;
    setState(() {
      _signInBusy = true;
      _accountOutcome = null;
    });
    try {
      final loginOrigin = serverOrigin(server);
      if (register) {
        final secret = _needsBootstrap ? _bootstrapSecret.text : null;
        String message;
        try {
          message = await widget.connection.register(
            server,
            key,
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
          _accountOutcome = ActionOutcome.ok(message);
        });
      } else {
        final token = await widget.connection
            .login(server, key, _username.text, _password.text);
        if (!mounted) return;
        _password.clear();
        AccountSession session;
        try {
          session = AccountSession(token: token, origin: loginOrigin);
        } on ArgumentError {
          setState(() => _accountOutcome = const ActionOutcome.failed(
              'The server did not return a usable session.'));
          return;
        }
        await _adoptSession(session);
      }
    } on BootstrapSecretRequired {
      if (!mounted) return;
      setState(() {
        _needsBootstrap = true;
        _accountOutcome = const ActionOutcome(
          StatusKind.warn,
          'The first administrator needs the bootstrap secret Sonder printed '
          'on the PC.',
          detail: 'Enter it as the bootstrap secret and register again. It '
              'is used once and never saved.',
        );
      });
    } on SonderException catch (e) {
      if (!mounted) return;
      final diagnosis = diagnoseConnectionError(e, server);
      final explained = diagnosis.state == ServerReachability.refused ||
          diagnosis.state == ServerReachability.rateLimited;
      setState(() {
        if (explained) _connection = diagnosis;
        _accountOutcome = ActionOutcome.failed(
            explained ? diagnosis.title : e.message,
            detail: explained && diagnosis.detail.isNotEmpty
                ? diagnosis.detail
                : null);
      });
    } on ArgumentError {
      if (!mounted) return;
      setState(() => _accountOutcome = const ActionOutcome.failed(
          'Sign-in needs an https:// server URL off this device.'));
    } catch (_) {
      if (!mounted) return;
      setState(() => _accountOutcome = const ActionOutcome.failed(
          'Account request could not be completed.'));
    } finally {
      if (mounted) setState(() => _signInBusy = false);
    }
  }

  /// A fresh sign-in. When it belongs to the saved server it is stored at
  /// once, in the secure store and bound to its exact origin
  /// ([Settings.storeAccountSession]), and handed to the app: no second
  /// Save. A sign-in to a server URL that is not saved yet stays staged, and
  /// Save stores both together.
  Future<void> _adoptSession(AccountSession session) async {
    final origin = session.origin;
    final user = _username.text.trim();
    _signedInUser = user.isEmpty ? null : user;
    _signedInToken = session.token;
    SettingsSaveResult? stored;
    if (session.matches(_saved.serverUrl)) {
      try {
        stored = await Settings.storeAccountSession(session,
            serverUrl: _saved.serverUrl);
      } on StateError {
        stored = null;
      }
    }
    if (!mounted) return;
    final result = stored;
    if (result == null) {
      setState(() {
        _account = session;
        _accountOutcome = ActionOutcome.ok('Signed in to $origin.',
            detail: 'Save to keep this session with the new server URL.');
      });
      return;
    }
    setState(() {
      _account = session;
      _saved = _saved.copyWith(accountSession: session);
      _accountOutcome = result.credentialsStored
          ? ActionOutcome.ok('Signed in to $origin.',
              detail: 'The session is stored in the system keyring.')
          : result.memoryOnly
              ? ActionOutcome.ok('Signed in to $origin.',
                  detail: 'The browser keeps the session in memory until '
                      'the page reloads.')
              : ActionOutcome(StatusKind.warn,
                  'Signed in to $origin, but the system keyring is unavailable.',
                  detail: 'The session lasts until the app closes.');
    });
    widget.onChanged(_saved.copyWith());
  }

  void _dropSession() {
    _account = null;
    _signedInUser = null;
    _signedInToken = null;
    _password.clear();
    _saved = _saved.copyWith(withoutAccountSession: true);
  }

  Future<void> _signOut() async {
    final account = _account;
    if (account == null || !account.matches(_server.text)) return;
    setState(() {
      _sessionBusy = true;
      _accountOutcome = null;
    });
    try {
      await widget.connection.logout(_key.text, account);
      await Settings.clearAccountSession();
      if (!mounted) return;
      setState(() {
        _dropSession();
        _accountOutcome = const ActionOutcome.ok('Signed out.',
            detail: 'The server revoked this session.');
      });
      widget.onChanged(_saved.copyWith());
    } catch (_) {
      if (!mounted) return;
      setState(() => _accountOutcome =
          const ActionOutcome.failed('Revocation not confirmed.',
              detail: 'Retry Sign out, or explicitly Forget local session. The '
                  'session is kept for the retry.'));
    } finally {
      if (mounted) setState(() => _sessionBusy = false);
    }
  }

  Future<void> _forgetSession() async {
    setState(() {
      _sessionBusy = true;
      _accountOutcome = null;
    });
    try {
      await Settings.clearAccountSession();
      if (!mounted) return;
      setState(() {
        _dropSession();
        _accountOutcome = const ActionOutcome.ok(
            'Account session forgotten locally.',
            detail: 'Server revocation was not requested.');
      });
      widget.onChanged(_saved.copyWith());
    } catch (_) {
      if (!mounted) return;
      setState(() => _accountOutcome = const ActionOutcome.failed(
          'Could not remove the account session securely.'));
    } finally {
      if (mounted) setState(() => _sessionBusy = false);
    }
  }

  // -- Save, theme -----------------------------------------------------------

  Future<void> _save(CategoryNavigator? pages) async {
    void refuse(String message, {String? page}) {
      setState(() => _saveError = message);
      if (page != null && pages != null && pages.selectedId != page) {
        pages.select(page);
      }
    }

    final account = _account;
    if (account != null && !account.matches(_server.text)) {
      refuse('Sign out on the account server, or forget the local session, '
          'before switching servers.');
      return;
    }
    final contextError = contextSizeError(_contextSize.text);
    if (contextError != null) {
      refuse('Context size: $contextError', page: SettingsCategory.general);
      return;
    }
    final s = _current();
    final launcherError = s.launcherConfigurationError;
    if (launcherError != null) {
      setState(() =>
          _launcherOutcome = ActionOutcome(StatusKind.warn, launcherError));
      refuse(launcherError, page: SettingsCategory.connection);
      return;
    }
    final observatoryError = s.observatoryConfigurationError;
    if (observatoryError != null) {
      refuse(observatoryError, page: SettingsCategory.observatory);
      return;
    }
    // A blank field explicitly replaces a credential that was saved. Do not
    // leave an old keychain value usable.
    SettingsSaveResult result;
    try {
      if (_saved.apiKey.trim().isNotEmpty && s.apiKey.trim().isEmpty) {
        await Settings.clearApiKey();
      }
      if (_saved.launcherToken.trim().isNotEmpty &&
          s.launcherToken.trim().isEmpty) {
        await Settings.clearLauncherToken();
      }
      result = await s.save();
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _keyringWarning = 'System keyring unavailable: a removed key could '
            'not be deleted, so nothing was saved. Try again once the '
            'keyring works.';
        _saveError = 'Nothing was saved: the system keyring is unavailable.';
      });
      return;
    }
    // A connection check still running restores the policy just saved.
    _keyPolicy.saved(s.cleartextKeyHosts);
    if (!mounted) return;
    widget.onChanged(s);
    if (parseContextTokens(_contextSize.text) != null) {
      _setText(_contextSize, s.contextSize);
    }
    setState(() {
      _saved = s.copyWith();
      _keyringWarning = result.warning;
      _saveError = null;
      _contextNote = null;
      _contextEdited = false;
    });
    showSonderToast(
      context,
      result.warning == null
          ? 'Settings saved'
          : 'Settings saved; keys kept in memory only',
      kind: result.warning == null ? StatusKind.ok : StatusKind.warn,
    );
  }

  /// The theme applies and persists at once: it carries no credential, so
  /// it never waits for Save.
  Future<void> _setTheme(String mode) async {
    if (mode == _saved.themeMode) return;
    setState(() => _saved = _saved.copyWith(themeMode: mode));
    widget.onChanged(_saved.copyWith());
    try {
      await Settings.saveThemeMode(mode);
    } catch (_) {
      if (mounted) {
        showSonderToast(context, "The theme changed but couldn't be saved.",
            kind: StatusKind.warn);
      }
    }
  }

  Future<void> _copyServerSetting(String setting) async {
    await Clipboard.setData(ClipboardData(text: setting));
    if (mounted) showSonderToast(context, 'Copied $setting');
  }

  // -- Layout ----------------------------------------------------------------

  /// "Open Runtime", where navigation exists (a shell, or the old routes).
  Widget? _runtimeLink(String label) {
    if (!_canNavigate) return null;
    return TextButton(
      onPressed: () => _navigate(WorkspaceDestination.runtime),
      child: Text(label),
    );
  }

  List<SonderCategory> _categories() {
    final changes = _changes;
    Widget? unsaved(String id) => changes[id] == true
        ? const ModifiedDot(
            tooltip: 'Unsaved changes', semanticLabel: 'unsaved changes')
        : null;
    final account = _account;
    return [
      SonderCategory(
        id: SettingsCategory.general,
        label: 'General',
        icon: Icons.tune,
        description: 'Default model and context size.',
        keywords: const ['model', 'route', 'context', 'tokens', 'window'],
        badge: unsaved(SettingsCategory.general),
        builder: _generalPage,
      ),
      SonderCategory(
        id: SettingsCategory.connection,
        label: 'Connection',
        icon: Icons.lan_outlined,
        description: 'Server, API key and host launcher.',
        keywords: const [
          'server',
          'url',
          'api key',
          'https',
          'http',
          'launcher',
          'token',
          'test',
          'keyring',
        ],
        badge: unsaved(SettingsCategory.connection),
        builder: _connectionPage,
      ),
      SonderCategory(
        id: SettingsCategory.account,
        label: 'Account',
        icon: Icons.person_outline,
        description: account == null
            ? 'Sign in to this server.'
            : 'Signed in to ${Uri.tryParse(account.origin)?.host ?? account.origin}.',
        keywords: const [
          'login',
          'sign in',
          'sign out',
          'register',
          'username',
          'password',
          'session',
          'bootstrap',
        ],
        badge: unsaved(SettingsCategory.account),
        builder: _accountPage,
      ),
      SonderCategory(
        id: SettingsCategory.appearance,
        label: 'Appearance',
        icon: Icons.palette_outlined,
        description: 'Light, dark or system theme.',
        keywords: const ['theme', 'dark', 'light', 'system', 'colour'],
        builder: _appearancePage,
      ),
      SonderCategory(
        id: SettingsCategory.privacy,
        label: 'Privacy',
        icon: Icons.shield_outlined,
        description: LocalManager.canRunLocalTools
            ? 'Location, cloud tiers and stored keys.'
            : 'Location and stored keys.',
        keywords: const ['location', 'ip', 'cloud', 'hosted', 'keyring'],
        badge: unsaved(SettingsCategory.privacy),
        builder: _privacyPage,
      ),
      if (LocalManager.canRunLocalTools)
        SonderCategory(
          id: SettingsCategory.desktop,
          label: 'Desktop',
          icon: Icons.desktop_windows_outlined,
          description: 'The server this app starts.',
          keywords: const ['local server', 'background', 'headless'],
          badge: unsaved(SettingsCategory.desktop),
          builder: _desktopPage,
        ),
      SonderCategory(
        id: SettingsCategory.observatory,
        label: 'Observatory',
        icon: Icons.insights_outlined,
        description: 'Where Runtime opens the Observatory.',
        keywords: const ['executable', 'web url', 'path', 'cors'],
        badge: unsaved(SettingsCategory.observatory),
        builder: _observatoryPage,
      ),
      SonderCategory(
        id: SettingsCategory.about,
        label: 'About',
        icon: Icons.info_outline,
        description: 'Version and server.',
        keywords: const ['version', 'build', 'platform'],
        builder: _aboutPage,
      ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final shell = ShellScope.maybeOf(context);
    final Widget? leading;
    double? leadingWidth;
    final List<Widget> actions;
    if (shell != null) {
      // The shell's sidebar or drawer owns navigation.
      leading = shell.sidebarVisible
          ? null
          : IconButton(
              tooltip: 'Open navigation',
              icon: const Icon(Icons.menu),
              onPressed: shell.openNavigation,
            );
      actions = const [];
    } else {
      // One return control (plan P2-11), at the leading edge so its tooltip
      // never collides with the window's own Close tooltip.
      leading = Padding(
        padding: const EdgeInsets.only(left: SonderSpace.sm),
        child: Tooltip(
          message: 'Back to chat',
          child: TextButton.icon(
            onPressed: _leaveSettings,
            icon: const Icon(Icons.arrow_back, size: 20),
            label: const Text('Chat'),
          ),
        ),
      );
      leadingWidth = 104;
      actions = [
        if (widget.onNavigate != null)
          WorkspaceMenu(
              current: WorkspaceDestination.settings, onSelected: _navigate),
      ];
    }
    final dirty = _dirty;
    return ShellLeaveGuard(
        canLeave: _leaveGuard,
        child: CategoryScaffold(
          key: ValueKey('settings-sections-$_sectionEpoch'),
          title: 'Settings',
          categories: _categories(),
          initialId: _initialCategory,
          leading: leading,
          leadingWidth: leadingWidth,
          actions: actions,
          searchHint: 'Search settings',
          contentMaxWidth: _contentWidth,
          navigationKey: const Key('settings-categories'),
          bottomBar: Builder(
            builder: (context) {
              final pages = CategoryNavigator.maybeOf(context);
              return UnsavedChangesBar(
                visible: dirty,
                where: _changedPages,
                error: _saveError,
                contentMaxWidth: _contentWidth,
                onDiscard: _discard,
                onSave: () => _save(pages),
              );
            },
          ),
        ));
  }
}

/// The plain-HTTP key allowance while Settings checks unsaved values.
///
/// "Test connection" and the model list use this screen's unsaved per-host
/// choice for the host they contact, and only that host: every other host
/// keeps the saved policy, so requests elsewhere in the app are unaffected.
/// Overlapping checks share one restore point, and the saved policy comes
/// back when the last one ends, whatever happened (Save during a check
/// updates the restore point instead of being undone).
class _StagedKeyPolicy {
  final Set<String> Function() _staged;
  final _hosts = <String, int>{};
  Set<String>? _restore;

  _StagedKeyPolicy(this._staged);

  Future<T> run<T>(String serverUrl, Future<T> Function() request) async {
    final host = CleartextKeyPolicy.hostKeyOf(serverUrl);
    _restore ??= {...CleartextKeyPolicy.allowedHosts};
    _hosts.update(host, (n) => n + 1, ifAbsent: () => 1);
    _apply();
    try {
      return await request();
    } finally {
      final left = _hosts[host]! - 1;
      if (left == 0) {
        _hosts.remove(host);
      } else {
        _hosts[host] = left;
      }
      if (_hosts.isEmpty) {
        CleartextKeyPolicy.allowOnly(_restore!);
        _restore = null;
      } else {
        _apply();
      }
    }
  }

  /// Settings were saved while checks run: restore to the new policy.
  void saved(Iterable<String> hosts) {
    if (_restore == null) return;
    _restore = {...hosts};
    _apply();
  }

  void _apply() {
    final staged = _staged();
    final allowed = {..._restore!};
    for (final host in _hosts.keys) {
      if (host.isEmpty) continue;
      allowed.remove(host);
      if (staged.contains(host)) allowed.add(host);
    }
    CleartextKeyPolicy.allowOnly(allowed);
  }
}
