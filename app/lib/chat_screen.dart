import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'agent_screen.dart';
import 'api.dart';
import 'app_control.dart';
import 'app_control_screen.dart';
import 'chat/backend.dart';
import 'chat/composer.dart';
import 'chat/connection.dart';
import 'chat/controller.dart';
import 'chat/drawer.dart';
import 'chat/empty_state.dart';
import 'chat/permission_mode.dart';
import 'chat/status_strip.dart';
import 'chat/transcript.dart';
import 'models.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'system_screen.dart';
import 'theme.dart';
import 'workspace_ui.dart';

class _OpenCommandBrowserIntent extends Intent {
  const _OpenCommandBrowserIntent();
}

class _NewChatIntent extends Intent {
  const _NewChatIntent();
}

class _OpenThreadSwitcherIntent extends Intent {
  const _OpenThreadSwitcherIntent();
}

class _OpenSettingsIntent extends Intent {
  const _OpenSettingsIntent();
}

class _OpenRuntimeIntent extends Intent {
  const _OpenRuntimeIntent();
}

class _CyclePermissionModeIntent extends Intent {
  const _CyclePermissionModeIntent();
}

/// Builds the server seam for a settings snapshot. Tests pass a double.
typedef ChatBackendFactory = ChatBackend Function(Settings settings);

/// The chat workspace shell: layout, navigation, shortcuts and the composer
/// wiring. State and behaviour live in [ChatController] (`lib/chat/`).
class ChatScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onSettingsChanged;
  final ChatBackendFactory? backendFactory;

  const ChatScreen({
    super.key,
    required this.settings,
    required this.onSettingsChanged,
    this.backendFactory,
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  late final AppControlClient _appControl;
  late AppControlContext _controlScope;
  late final ChatController _chat;
  late String _identity;

  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _inputFocus = FocusNode();

  List<SonderCommand> _paletteMatches = const [];
  int _paletteSelected = 0;
  bool _paletteGrouped = false;
  int _seenEntries = 0;
  int _seenPendingLength = 0;

  AppControlContext _contextFor(Settings settings) => AppControlContext(
        serverUrl: settings.serverUrl,
        deploymentKey: settings.apiKey,
        account: settings.accountSession,
      );

  /// Server, key and account: a change means a different principal, so the
  /// read-only mode chip and cached state reset.
  static String _identityOf(Settings s) =>
      '${s.serverUrl}\u0000${s.apiKey}\u0000${s.accountSession?.token ?? ''}';

  /// The one [SonderApi] for the current server identity. Chat turns,
  /// Stop, feedback, work runs, approvals and the Agents page share it; it
  /// is replaced only when the server, key or account changes. (Building a
  /// fresh instance per access made Stop cancel nothing.)
  late SonderApi _api = _apiFor(widget.settings);

  static SonderApi _apiFor(Settings s) => SonderApi(
        baseUrl: s.serverUrl,
        apiKey: s.apiKey,
        accountSession: s.accountSession,
      );

  ChatBackend _backendFor(Settings s) =>
      widget.backendFactory?.call(s) ?? SonderApiChatBackend.withApi(_api);

  void _controlChanged() {
    if (mounted) setState(() {});
  }

  @override
  void initState() {
    super.initState();
    _controlScope = _contextFor(widget.settings);
    _appControl = AppControlClient(context: () => _contextFor(widget.settings));
    _appControl.addListener(_controlChanged);
    _identity = _identityOf(widget.settings);
    _chat = ChatController(_backendFor(widget.settings),
        model: widget.settings.model)
      ..contextSize = widget.settings.contextSize
      ..allowApproximateLocation = widget.settings.allowApproximateLocation
      ..onAnnounce = (message) {
        if (mounted) announceToScreenReader(context, message);
      };
    _chat.addListener(_chatChanged);
    unawaited(_chat.start());
    WidgetsBinding.instance.addObserver(this);
    _input.addListener(_updatePalette);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);
    _chat.handleLifecycle(state);
  }

  @override
  void didUpdateWidget(covariant ChatScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    _controlScope = _contextFor(widget.settings);
    _appControl.synchronize();
    _syncSettings();
  }

  void _syncSettings() {
    final s = widget.settings;
    _chat
      ..contextSize = s.contextSize
      ..allowApproximateLocation = s.allowApproximateLocation;
    final identity = _identityOf(s);
    final changed = identity != _identity;
    _identity = identity;
    if (changed) _api = _apiFor(s);
    _chat.updateBackend(_backendFor(s), identityChanged: changed);
    _chat.syncModel(s.model);
  }

  @override
  void dispose() {
    _appControl.removeListener(_controlChanged);
    _appControl.dispose();
    _chat.removeListener(_chatChanged);
    _chat.dispose();
    WidgetsBinding.instance.removeObserver(this);
    _input.removeListener(_updatePalette);
    _input.dispose();
    _scroll.dispose();
    _inputFocus.dispose();
    super.dispose();
  }

  /// Follow a growing transcript, but only while the reader is at the end.
  void _chatChanged() {
    final entries = _chat.entries;
    final pendingLength = entries.isNotEmpty && entries.last.message.pending
        ? entries.last.message.content.length
        : 0;
    if (entries.length != _seenEntries || pendingLength != _seenPendingLength) {
      final grew = entries.length > _seenEntries;
      _seenEntries = entries.length;
      _seenPendingLength = pendingLength;
      if (grew || pendingLength > 0) scrollTranscriptToEnd(_scroll);
    }
    // The palette follows the catalog when it lands.
    if (_input.text.startsWith('/')) _updatePalette(force: true);
  }

  // -- Palette ---------------------------------------------------------------

  void _updatePalette({bool force = false}) {
    final text = _input.text;
    List<SonderCommand> matches = const [];
    var grouped = false;
    final catalog = _chat.catalog;
    if (text.startsWith('/') && !text.contains(RegExp(r'[\s\n]'))) {
      final query = text.toLowerCase();
      if (query == '/') {
        matches = catalog.popularCommands;
        grouped = true;
      } else {
        matches =
            catalog.commands.where((c) => c.matchesPrefix(query)).toList();
        if (matches.isEmpty) {
          final needle = query.substring(1);
          matches =
              catalog.commands.where((c) => c.matchesLoose(needle)).toList();
        }
      }
    }
    if (!force &&
        grouped == _paletteGrouped &&
        matches.length == _paletteMatches.length &&
        (matches.isEmpty || matches.first.name == _paletteMatches.first.name)) {
      return;
    }
    if (!mounted) return;
    setState(() {
      _paletteMatches = matches;
      _paletteGrouped = grouped;
      if (!force) _paletteSelected = 0;
      if (_paletteSelected >= matches.length) _paletteSelected = 0;
    });
  }

  void _pickCommand(String command) {
    final insert = command.contains(' ') ? command : '$command ';
    _input.value = TextEditingValue(
      text: insert,
      selection: TextSelection.collapsed(offset: insert.length),
    );
    setState(() {
      _paletteMatches = const [];
      _paletteGrouped = false;
      _paletteSelected = 0;
    });
    _inputFocus.requestFocus();
  }

  KeyEventResult _onComposerKey(KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    final key = event.logicalKey;
    if (_paletteMatches.isEmpty && key == LogicalKeyboardKey.enter) {
      if (HardwareKeyboard.instance.isShiftPressed) {
        final value = _input.value;
        final start = value.selection.start < 0
            ? value.text.length
            : value.selection.start;
        final end = value.selection.end < 0 ? start : value.selection.end;
        _input.value = value.copyWith(
          text: value.text.replaceRange(start, end, '\n'),
          selection: TextSelection.collapsed(offset: start + 1),
          composing: TextRange.empty,
        );
      } else {
        _submit();
      }
      return KeyEventResult.handled;
    }
    if (_paletteMatches.isEmpty) return KeyEventResult.ignored;
    if (key == LogicalKeyboardKey.arrowDown) {
      setState(() =>
          _paletteSelected = (_paletteSelected + 1) % _paletteMatches.length);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.arrowUp) {
      setState(() => _paletteSelected =
          (_paletteSelected - 1 + _paletteMatches.length) %
              _paletteMatches.length);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.enter || key == LogicalKeyboardKey.tab) {
      _pickCommand(_paletteMatches[_paletteSelected].name);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.escape) {
      setState(() {
        _paletteMatches = const [];
        _paletteGrouped = false;
      });
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  // -- Send, intercepts, cancel ------------------------------------------

  void _submit([String? preset]) {
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty) return;
    final intercept = classifyIntercept(text);
    if (intercept != null) {
      // Cleared first: a typed password must not linger in the composer.
      if (preset == null) _input.clear();
      unawaited(_handleIntercept(intercept));
      return;
    }
    if (_chat.sending) return;
    if (preset == null) _input.clear();
    unawaited(_chat.send(text).whenComplete(() {
      if (mounted) _inputFocus.requestFocus();
    }));
    scrollTranscriptToEnd(_scroll, force: true);
  }

  Future<void> _handleIntercept(ComposerIntercept intercept) async {
    switch (intercept) {
      case AccountIntercept(:final command):
        final messenger = ScaffoldMessenger.of(context);
        messenger.showSnackBar(SnackBar(
          content: Text(command == '/register'
              ? 'Create accounts in Settings > Account. Passwords never go '
                  'into the chat.'
              : 'Sign in from Settings > Account. Passwords never go into '
                  'the chat.'),
        ));
        await _openSettings();
      case ModeIntercept(:final target):
        if (target == null) {
          await _openPermissionModePicker();
        } else {
          await _changeMode(target);
        }
    }
  }

  void _cancelSend() {
    final text = _chat.cancel();
    if (text != null && _input.text.trim().isEmpty) {
      _input.value = TextEditingValue(
          text: text, selection: TextSelection.collapsed(offset: text.length));
    }
  }

  // -- Permission mode ---------------------------------------------------

  Future<void> _openPermissionModePicker() async {
    final current = _chat.permissionMode;
    if (current == null) {
      _snack(_chat.connection.value.isOffline
          ? "The mode can't be changed while the server is unreachable."
          : 'This server does not report a permission mode.');
      return;
    }
    if (_chat.modeReadOnly) {
      _snack(modeReadOnlyText);
      return;
    }
    if (_chat.switchingMode) return;
    final picked = await showDialog<String>(
      context: context,
      builder: (_) => PermissionModeDialog(state: current),
    );
    if (picked == null || !mounted) return;
    await _changeMode(picked);
  }

  Future<void> _changeMode(String target) async {
    final (outcome, message) = await _chat.requestModeChange(
      target,
      confirm: (from, to) => confirmModeRaise(context,
          from: from,
          to: to,
          host: ConnectionStatus.hostOf(widget.settings.serverUrl)),
    );
    if (!mounted) return;
    if (outcome == ModeChangeOutcome.failed ||
        outcome == ModeChangeOutcome.readOnly) {
      _snack(message);
    }
  }

  void _cycleMode() {
    final next = _chat.nextModeInCycle();
    if (next == null) return;
    unawaited(_changeMode(next));
  }

  void _snack(String message) {
    if (message.isEmpty) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  // -- Navigation -------------------------------------------------------------

  Future<void> _openCommandBrowser() async {
    final picked = await showDialog<String>(
      context: context,
      builder: (_) => CommandBrowser(
          catalog: _chat.catalog, fromServer: _chat.catalogFromServer),
    );
    if (picked == null || !mounted) return;
    _pickCommand(picked);
  }

  void _selectModel(String m) {
    _chat.selectModel(m);
    widget.settings.model = m;
    widget.settings.save();
  }

  Future<void> _editProject() async {
    final controller = TextEditingController(text: _chat.project);
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Project'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: 'Project name',
            hintText: 'default, app-ui, engine...',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(controller.text),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (value == null) return;
    await _chat.saveCurrentThread(project: value);
  }

  Future<void> _openSettings() async {
    await Navigator.of(context).push(
      PageRouteBuilder<void>(
        transitionDuration: Duration.zero,
        reverseTransitionDuration: Duration.zero,
        pageBuilder: (_, __, ___) => SettingsScreen(
          settings: widget.settings,
          onChanged: (next) {
            final scope = _contextFor(next);
            if (!_controlScope.same(scope)) _appControl.forget();
            _controlScope = scope;
            widget.onSettingsChanged(next);
          },
          onNavigate: _navigateWorkspace,
        ),
        transitionsBuilder: (_, __, ___, child) => child,
      ),
    );
    if (!mounted) return;
    // Settings may have mutated the same object in place.
    _syncSettings();
  }

  Future<void> _openRuntime() async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => SystemScreen(
            settings: widget.settings, onNavigate: _navigateWorkspace),
      ),
    );
  }

  void _navigateWorkspace(WorkspaceDestination destination) {
    Navigator.of(context).popUntil((route) => route.isFirst);
    switch (destination) {
      case WorkspaceDestination.chat:
        return;
      case WorkspaceDestination.agents:
        unawaited(_openAgents());
      case WorkspaceDestination.runtime:
        unawaited(_openRuntime());
      case WorkspaceDestination.settings:
        unawaited(_openSettings());
    }
  }

  Future<void> _openAgents() => Navigator.of(context).push<void>(
        MaterialPageRoute(
            builder: (_) =>
                AgentScreen(api: _api, onNavigate: _navigateWorkspace)),
      );

  Future<void> _openAppControl() => Navigator.of(context).push<void>(
        MaterialPageRoute(
            builder: (_) => AppControlScreen(
                  client: _appControl,
                  initialProject: _chat.project,
                  localHistoryAlias: _chat.currentThreadId,
                  onSettings: () {
                    Navigator.of(context).pop();
                    _openSettings();
                  },
                )),
      );

  void _switchThread(ChatThread thread) {
    _chat.switchThread(thread);
    unawaited(Navigator.of(context).maybePop());
    scrollTranscriptToEnd(_scroll, force: true);
  }

  Future<void> _openThreadSwitcher(bool desktop) async {
    Widget drawer() => ChatDrawer(
          threads: _chat.threads,
          currentThreadId: _chat.currentThreadId,
          onNew: _chat.newChat,
          onSelect: _switchThread,
          onDelete: _chat.deleteThread,
        );
    if (!desktop) {
      await showModalBottomSheet<void>(
        context: context,
        builder: (_) => SizedBox(height: 560, child: drawer()),
      );
      return;
    }
    await showDialog<void>(
      context: context,
      builder: (_) => Dialog(
        child: SizedBox(width: 360, height: 600, child: drawer()),
      ),
    );
  }

  String _modelLabel(String m) => m == 'sonder' ? 'sonder (local route)' : m;

  TranscriptActions get _transcriptActions => TranscriptActions(
        onStop: _cancelSend,
        onFeedback: (command) => unawaited(_chat.recordFeedback(command)),
        onRetry: (id) => unawaited(_chat.retry(id)),
        onChangeMode: () => unawaited(_openPermissionModePicker()),
        onApprove: (callId, ttl) => _chat.backend.approveCall(callId, ttl: ttl),
        fetchWorkRun: (id) => _chat.backend.getWorkRun(id),
        cancelWorkRun: (id) => _chat.backend.cancelWorkRun(id),
        listWorkRuns: () => _chat.backend.listWorkRuns(),
        onWorkRunResolved: (entryId, run) =>
            unawaited(_chat.resolveWorkRun(entryId, run)),
      );

  Widget? _modeChip() {
    final mode = _chat.permissionMode;
    if (mode != null) {
      return PermissionModeChip(
        state: mode,
        busy: _chat.switchingMode,
        readOnly: _chat.modeReadOnly,
        onTap: () => unawaited(_openPermissionModePicker()),
      );
    }
    if (_chat.connection.value.isOffline && _chat.lastKnownMode != null) {
      return const OfflineModeChip();
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _chat,
      builder: (context, _) => _buildScreen(context),
    );
  }

  Widget _buildScreen(BuildContext context) {
    final currentTitle = _chat.loadingThreads
        ? 'Loading chats...'
        : _chat.currentThread.displayTitle;
    final entries = _chat.entries;
    return LayoutBuilder(
      builder: (context, constraints) {
        final desktop = constraints.maxWidth >= 1000;
        final compact = constraints.maxWidth < 600;
        final drawer = ChatDrawer(
          threads: _chat.threads,
          currentThreadId: _chat.currentThreadId,
          onNew: _chat.newChat,
          onSelect: _switchThread,
          onDelete: _chat.deleteThread,
          embedded: desktop,
          onNavigate: _navigateWorkspace,
          connection: _chat.connection,
          onOpenCommands: desktop ? _openCommandBrowser : null,
          onOpenRuntime: desktop ? _openRuntime : null,
          onOpenSettings: desktop ? _openSettings : null,
        );
        return Shortcuts(
          shortcuts: const <ShortcutActivator, Intent>{
            SingleActivator(LogicalKeyboardKey.keyK, control: true):
                _OpenCommandBrowserIntent(),
            SingleActivator(LogicalKeyboardKey.keyN, control: true):
                _NewChatIntent(),
            SingleActivator(LogicalKeyboardKey.keyP, control: true):
                _OpenThreadSwitcherIntent(),
            SingleActivator(LogicalKeyboardKey.comma, control: true):
                _OpenSettingsIntent(),
            SingleActivator(LogicalKeyboardKey.keyD, control: true):
                _OpenRuntimeIntent(),
            SingleActivator(LogicalKeyboardKey.tab, shift: true):
                _CyclePermissionModeIntent(),
          },
          child: Actions(
            actions: <Type, Action<Intent>>{
              _OpenCommandBrowserIntent:
                  CallbackAction<_OpenCommandBrowserIntent>(onInvoke: (_) {
                unawaited(_openCommandBrowser());
                return null;
              }),
              _NewChatIntent: CallbackAction<_NewChatIntent>(onInvoke: (_) {
                _chat.newChat();
                return null;
              }),
              _OpenThreadSwitcherIntent:
                  CallbackAction<_OpenThreadSwitcherIntent>(onInvoke: (_) {
                unawaited(_openThreadSwitcher(desktop));
                return null;
              }),
              _OpenSettingsIntent:
                  CallbackAction<_OpenSettingsIntent>(onInvoke: (_) {
                unawaited(_openSettings());
                return null;
              }),
              _OpenRuntimeIntent:
                  CallbackAction<_OpenRuntimeIntent>(onInvoke: (_) {
                unawaited(_openRuntime());
                return null;
              }),
              _CyclePermissionModeIntent:
                  CallbackAction<_CyclePermissionModeIntent>(onInvoke: (_) {
                _cycleMode();
                return null;
              }),
            },
            child: Scaffold(
              drawer: desktop ? null : drawer,
              appBar: AppBar(
                titleSpacing: desktop ? 20 : 0,
                title: _ChatHeader(
                  title: currentTitle,
                  project: _chat.project,
                  messageCount: entries.where((e) => !e.message.pending).length,
                  onEditProject: _editProject,
                ),
                actions: [
                  IconButton(
                      tooltip: 'Server conversations',
                      icon: const Icon(Icons.link_outlined),
                      onPressed: _openAppControl),
                  ConstrainedBox(
                      constraints:
                          BoxConstraints(maxWidth: compact ? 100 : 260),
                      child: _ModelPill(
                        label: _modelLabel(_chat.model),
                        models: _chat.models,
                        current: _chat.model,
                        labelFor: _modelLabel,
                        onSelected: _selectModel,
                      )),
                  if (compact)
                    PopupMenuButton<String>(
                      tooltip: 'Chat actions',
                      onSelected: (action) {
                        switch (action) {
                          case 'commands':
                            _openCommandBrowser();
                          case 'new':
                            _chat.newChat();
                          case 'runtime':
                            _openRuntime();
                          case 'settings':
                            _openSettings();
                        }
                      },
                      itemBuilder: (_) => const [
                        PopupMenuItem(
                            value: 'commands', child: Text('Commands')),
                        PopupMenuItem(value: 'new', child: Text('New chat')),
                        PopupMenuItem(value: 'runtime', child: Text('Runtime')),
                        PopupMenuItem(
                            value: 'settings', child: Text('Settings')),
                      ],
                    ),
                  if (!desktop && !compact) ...[
                    const SizedBox(width: 4),
                    IconButton(
                      tooltip: 'Commands',
                      icon: const Icon(Icons.bolt_outlined),
                      onPressed: _openCommandBrowser,
                    ),
                    IconButton(
                      tooltip: 'New chat',
                      icon: const Icon(Icons.add_comment_outlined),
                      onPressed: _chat.newChat,
                    ),
                    IconButton(
                      tooltip: 'Runtime',
                      icon: const Icon(Icons.dashboard_customize_outlined),
                      onPressed: _openRuntime,
                    ),
                    IconButton(
                      tooltip: 'Settings',
                      icon: const Icon(Icons.settings_outlined),
                      onPressed: _openSettings,
                    ),
                  ],
                  const SizedBox(width: 8),
                ],
              ),
              body: Row(
                children: [
                  if (desktop) SizedBox(width: 272, child: drawer),
                  Expanded(
                    child: Column(
                      children: [
                        if (_appControl.hasSession &&
                            _appControl.selectionKnown &&
                            _appControl.selection?.bindingId != null)
                          Padding(
                              padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                              child: WorkspaceNotice(
                                message:
                                    'A server conversation is selected. Running tasks is not available yet.',
                                action: TextButton(
                                    onPressed: _openAppControl,
                                    child: const Text('Manage selection')),
                              )),
                        // Pinned while the server is unreachable and there is
                        // a conversation to read (P2-13); the empty state
                        // carries the same notice itself.
                        if (entries.isNotEmpty)
                          ValueListenableBuilder<ConnectionStatus>(
                            valueListenable: _chat.connection,
                            builder: (context, c, _) => !c.isOffline
                                ? const SizedBox.shrink()
                                : Padding(
                                    padding: const EdgeInsets.fromLTRB(
                                        16, 10, 16, 0),
                                    child: Center(
                                      child: ConstrainedBox(
                                        constraints: const BoxConstraints(
                                            maxWidth: conversationWidth),
                                        child: OfflineNotice(
                                          key: const Key('offline-notice'),
                                          status: c,
                                          onRetry: () => unawaited(
                                              _chat.retryConnection()),
                                          onSettings: () =>
                                              unawaited(_openSettings()),
                                        ),
                                      ),
                                    ),
                                  ),
                          ),
                        Expanded(
                          child: entries.isEmpty
                              ? ChatEmptyState(
                                  connection: _chat.connection,
                                  onQuick: _submit,
                                  onRetry: () =>
                                      unawaited(_chat.retryConnection()),
                                  onSettings: () => unawaited(_openSettings()),
                                )
                              : Transcript(
                                  entries: entries,
                                  scroll: _scroll,
                                  live: _chat.live,
                                  actions: _transcriptActions,
                                ),
                        ),
                        ChatComposer(
                          controller: _input,
                          focusNode: _inputFocus,
                          sending: _chat.sending,
                          onSend: () => _submit(),
                          onCancel: _cancelSend,
                          paletteMatches: _paletteMatches,
                          paletteSelected: _paletteSelected,
                          paletteGrouped: _paletteGrouped,
                          paletteCategories: _chat.catalog.categories,
                          onPalettePick: _pickCommand,
                          onKey: _onComposerKey,
                          modeChip: _modeChip(),
                          onOpenCommands: _openCommandBrowser,
                          desktop: desktop,
                        ),
                        ChatStatusStrip(
                          info: _chat.status,
                          mode: _chat.permissionMode,
                          model: _chat.model,
                          tier: _chat.lastTier,
                          project: _chat.project,
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

/// The model picker as a quiet pill.
class _ModelPill extends StatelessWidget {
  final String label;
  final List<String> models;
  final String current;
  final String Function(String) labelFor;
  final ValueChanged<String> onSelected;

  const _ModelPill({
    required this.label,
    required this.models,
    required this.current,
    required this.labelFor,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return PopupMenuButton<String>(
      tooltip: 'Choose inference route or model',
      onSelected: onSelected,
      position: PopupMenuPosition.under,
      itemBuilder: (_) => models
          .map((m) => PopupMenuItem<String>(
                value: m,
                child: Row(children: [
                  if (m == current)
                    Icon(Icons.check, size: 16, color: tokens.accent)
                  else
                    const SizedBox(width: 16),
                  const SizedBox(width: 10),
                  Text(labelFor(m), style: tokens.mono(13)),
                ]),
              ))
          .toList(),
      child: Container(
        height: 30,
        constraints: const BoxConstraints(maxWidth: 260),
        padding: const EdgeInsets.fromLTRB(10, 0, 6, 0),
        decoration: BoxDecoration(
          color: tokens.panel,
          borderRadius: BorderRadius.circular(SonderRadius.row),
          border: Border.all(color: tokens.hairline),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Flexible(
            child: Text(label,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(12, weight: FontWeight.w500)),
          ),
          const SizedBox(width: 4),
          Icon(Icons.expand_more, size: 16, color: tokens.muted),
        ]),
      ),
    );
  }
}

class _ChatHeader extends StatelessWidget {
  final String title;
  final String project;
  final int messageCount;
  final VoidCallback onEditProject;

  const _ChatHeader({
    required this.title,
    required this.project,
    required this.messageCount,
    required this.onEditProject,
  });

  String get _count => '$messageCount message${messageCount == 1 ? '' : 's'}';

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(builder: (context, constraints) {
      if (constraints.maxWidth < 320) {
        return Row(children: [
          Expanded(
              child: Tooltip(
                  message: '$title / $_count',
                  child: Text(title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: text.titleSmall))),
          IconButton(
              tooltip: 'Project: $project - tap to change',
              onPressed: onEditProject,
              icon: const Icon(Icons.folder_outlined)),
        ]);
      }
      return Row(
        children: [
          Flexible(
            child: Text(title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: text.titleSmall),
          ),
          const SizedBox(width: 10),
          Tooltip(
            message: 'Project: tap to change',
            child: InkWell(
              onTap: onEditProject,
              borderRadius: BorderRadius.circular(6),
              child: Container(
                height: 24,
                padding: const EdgeInsets.symmetric(horizontal: 8),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(6),
                  border: Border.all(color: tokens.hairline),
                ),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      color: tokens.accent,
                      borderRadius: BorderRadius.circular(3),
                    ),
                  ),
                  const SizedBox(width: 6),
                  ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 160),
                    child: Text(
                      project.trim().isEmpty ? 'default' : project,
                      overflow: TextOverflow.ellipsis,
                      style: tokens.mono(11, color: tokens.text2),
                    ),
                  ),
                ]),
              ),
            ),
          ),
          const SizedBox(width: 10),
          Flexible(
            child: Text(_count,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(11, color: tokens.muted)),
          ),
        ],
      );
    });
  }
}
