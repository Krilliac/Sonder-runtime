import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'agent_screen.dart';
import 'api.dart';
import 'app_control.dart';
import 'app_control_screen.dart';
import 'chat/composer.dart';
import 'chat/connection.dart';
import 'chat/controller.dart';
import 'chat/empty_state.dart';
import 'chat/permission_mode.dart';
import 'chat/status_strip.dart';
import 'chat/transcript.dart';
import 'runtime/model_routing.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'shell/chat_session.dart';
import 'system_screen.dart';
import 'theme.dart';
import 'ui/kit.dart';
import 'workspace_ui.dart';

export 'shell/chat_session.dart' show ChatBackendFactory, ChatSession;

/// The Chat destination: the conversation header, the transcript, the
/// composer and the status strip. Navigation lives in the app shell
/// (`lib/shell/`); state and behaviour live in [ChatController]
/// (`lib/chat/`), held by a [ChatSession].
class ChatScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onSettingsChanged;
  final ChatBackendFactory? backendFactory;

  /// The chat session to show. The app shell owns one, so its sidebar
  /// shares the threads and a streaming turn outlives this page being
  /// hidden. Null (tests that pump the page alone): the page makes and owns
  /// its own, built with [backendFactory].
  final ChatSession? session;

  const ChatScreen({
    super.key,
    required this.settings,
    required this.onSettingsChanged,
    this.backendFactory,
    this.session,
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen>
    with WidgetsBindingObserver
    implements ChatPageHandle {
  late final ChatSession _session = widget.session ??
      ChatSession(
          settings: widget.settings, backendFactory: widget.backendFactory);
  late final bool _ownsSession = widget.session == null;

  ChatController get _chat => _session.chat;
  AppControlClient get _appControl => _session.appControl;

  /// The one [SonderApi] for the current server identity, shared with the
  /// session (see [ChatSession.api]).
  SonderApi get _api => _session.api;

  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _inputFocus = FocusNode();

  List<SonderCommand> _paletteMatches = const [];
  int _paletteSelected = 0;
  bool _paletteGrouped = false;
  int _seenEntries = 0;
  int _seenPendingLength = 0;
  String _seenThreadId = '';

  void _controlChanged() {
    if (mounted) setState(() {});
  }

  @override
  void initState() {
    super.initState();
    if (_ownsSession) {
      _chat.onAnnounce = (message) {
        if (mounted) announceToScreenReader(context, message);
      };
      WidgetsBinding.instance.addObserver(this);
    }
    _session.page = this;
    _appControl.addListener(_controlChanged);
    _chat.addListener(_chatChanged);
    _session.start();
    _input.addListener(_updatePalette);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);
    // A shell-owned session follows the shell's lifecycle observer.
    if (_ownsSession) _session.handleLifecycle(state);
  }

  @override
  void didUpdateWidget(covariant ChatScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (_ownsSession) _session.syncSettings(widget.settings);
  }

  @override
  void dispose() {
    _appControl.removeListener(_controlChanged);
    _chat.removeListener(_chatChanged);
    if (identical(_session.page, this)) _session.page = null;
    if (_ownsSession) {
      WidgetsBinding.instance.removeObserver(this);
      _session.dispose();
    }
    _input.removeListener(_updatePalette);
    _input.dispose();
    _scroll.dispose();
    _inputFocus.dispose();
    super.dispose();
  }

  // -- Shell hooks (ChatPageHandle) -------------------------------------------

  @override
  void openCommandBrowser() => unawaited(_openCommandBrowser());

  @override
  void focusComposer() {
    if (mounted) _inputFocus.requestFocus();
  }

  /// Follow a growing transcript, but only while the reader is at the end;
  /// a different conversation opens at its end.
  void _chatChanged() {
    final entries = _chat.entries;
    final threadId = _chat.currentThreadId;
    if (threadId != _seenThreadId) {
      // The first load keeps the reader's position rules below; switching
      // to another conversation (sidebar, search) opens it at its end.
      final switched = _seenThreadId.isNotEmpty;
      _seenThreadId = threadId;
      if (switched) {
        _seenEntries = entries.length;
        _seenPendingLength = 0;
        scrollTranscriptToEnd(_scroll, force: true);
      }
    }
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
    // Shift+Tab cycles the permission mode here in the composer only (with
    // the palette closed), so it moves focus backwards everywhere else.
    // Raising the mode still goes through the raise sheet.
    if (key == LogicalKeyboardKey.tab &&
        HardwareKeyboard.instance.isShiftPressed &&
        _paletteMatches.isEmpty) {
      return _cycleMode() ? KeyEventResult.handled : KeyEventResult.ignored;
    }
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
        _go(WorkspaceDestination.settings, section: 'account');
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
      confirm: (from, to) => confirmModeChange(context,
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

  /// Move to the next mode; false when there is no mode to cycle (the
  /// server has not said, or is unreachable).
  bool _cycleMode() {
    final next = _chat.nextModeInCycle();
    if (next == null) return false;
    unawaited(_changeMode(next));
    return true;
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

  /// Go to a peer destination, optionally at one of its sections (a
  /// Settings category, an agent lane). Inside the app shell the shell does
  /// it and runs its leave guards; pumped alone, this page pushes the
  /// destination as a route.
  void _go(WorkspaceDestination destination, {String? section}) {
    final shell = context.getInheritedWidgetOfExactType<ShellScope>();
    if (shell != null) {
      final open = shell.openSection;
      if (section != null && open != null) {
        open(destination, section);
      } else {
        shell.navigate(destination);
      }
      return;
    }
    switch (destination) {
      case WorkspaceDestination.chat:
        return;
      case WorkspaceDestination.agents:
        unawaited(_pushAgents(initialLaneId: section));
      case WorkspaceDestination.runtime:
        unawaited(_pushRuntime());
      case WorkspaceDestination.settings:
        unawaited(_pushSettings());
    }
  }

  /// The New chat button on narrow layouts. An untouched new chat is
  /// already open: keep it rather than pile up empty conversations.
  void _newChat() {
    final untouched = _chat.entries.isEmpty &&
        !_chat.sending &&
        _chat.currentThread.messages.isEmpty;
    if (!untouched) _chat.newChat();
  }

  // Pushed routes, for this page outside the shell (tests pump it alone).

  Future<void> _pushSettings() async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => SettingsScreen(
          settings: widget.settings,
          onChanged: (next) {
            _session.settingsSaved(next);
            widget.onSettingsChanged(next);
          },
          onNavigate: _pushedNavigate,
        ),
      ),
    );
    if (!mounted) return;
    // Settings may have mutated the same object in place.
    _session.syncSettings(widget.settings);
  }

  Future<void> _pushRuntime() => Navigator.of(context).push<void>(
        MaterialPageRoute(
          builder: (_) => SystemScreen(
              settings: widget.settings, onNavigate: _pushedNavigate),
        ),
      );

  Future<void> _pushAgents({String? initialLaneId}) =>
      Navigator.of(context).push<void>(
        MaterialPageRoute(
            builder: (_) => AgentScreen(
                api: _api,
                initialLaneId: initialLaneId,
                initialProject: _chat.project,
                onNavigate: _pushedNavigate)),
      );

  void _pushedNavigate(WorkspaceDestination destination) {
    Navigator.of(context).popUntil((route) => route.isFirst);
    _go(destination);
  }

  Future<void> _openAppControl() => Navigator.of(context).push<void>(
        MaterialPageRoute(
            builder: (_) => AppControlScreen(
                  client: _appControl,
                  initialProject: _chat.project,
                  localHistoryAlias: _chat.currentThreadId,
                  onSettings: () {
                    Navigator.of(context).pop();
                    _go(WorkspaceDestination.settings, section: 'account');
                  },
                )),
      );

  String _modelLabel(String m) => _chat.routing.pickerLabel(m);

  TranscriptActions get _transcriptActions => TranscriptActions(
        onStop: _cancelSend,
        onFeedback: (command) => unawaited(_chat.recordFeedback(command)),
        onRetry: (id) => unawaited(_chat.retry(id)),
        onChangeMode: () => unawaited(_openPermissionModePicker()),
        onLookupApproval: (callId) => _chat.backend.lookupPendingCall(callId),
        onApprove: (callId, ttl, tool, digest) => _chat.backend
            .approveCall(callId, ttl: ttl, tool: tool, digest: digest),
        fetchWorkRun: (id) => _chat.backend.getWorkRun(id),
        cancelWorkRun: (id) => _chat.backend.cancelWorkRun(id),
        listWorkRuns: () => _chat.backend.listWorkRuns(),
        onWorkRunResolved: (entryId, run) =>
            unawaited(_chat.resolveWorkRun(entryId, run)),
        onOpenAgentLane: (id) =>
            _go(WorkspaceDestination.agents, section: id),
        onSendCommand: _submit,
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
    final shell = ShellScope.maybeOf(context);
    // Narrow layouts inside the shell: the sidebar is a drawer behind the
    // menu button, and New chat stays one tap away in the header.
    final menu = shell != null && !shell.sidebarVisible;
    // The composer's keyboard hints follow the window, not this pane.
    final desktop = MediaQuery.sizeOf(context).width >= 1000;
    return LayoutBuilder(
      builder: (context, constraints) {
        final compact = constraints.maxWidth < 600;
        return Scaffold(
          appBar: AppBar(
            automaticallyImplyLeading: false,
            leading: menu ? const ShellMenuButton() : null,
            titleSpacing:
                menu ? 0 : (compact ? SonderSpace.lg : SonderSpace.xl),
            title: _ChatHeader(
              title: currentTitle,
              project: _chat.project,
              messageCount: entries.where((e) => !e.message.pending).length,
              onEditProject: _editProject,
            ),
            actions: [
              if (menu)
                IconButton(
                  key: const Key('chat-new-chat'),
                  tooltip: 'New chat',
                  icon: const Icon(Icons.add_comment_outlined),
                  onPressed: _newChat,
                ),
              IconButton(
                  tooltip: 'Server conversations',
                  icon: const Icon(Icons.link_outlined),
                  onPressed: _openAppControl),
              ConstrainedBox(
                  constraints: BoxConstraints(maxWidth: compact ? 100 : 260),
                  child: _ModelPill(
                    label: _modelLabel(_chat.model),
                    models: _chat.models,
                    current: _chat.model,
                    routing: _chat.routing,
                    onSelected: _selectModel,
                  )),
              const SizedBox(width: SonderSpace.sm),
            ],
          ),
          body: Column(
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
              // Pinned while the server is unreachable and there is a
              // conversation to read (P2-13); the empty state carries the
              // same notice itself.
              if (entries.isNotEmpty)
                ValueListenableBuilder<ConnectionStatus>(
                  valueListenable: _chat.connection,
                  builder: (context, c, _) => !c.isOffline
                      ? const SizedBox.shrink()
                      : Padding(
                          padding: const EdgeInsets.fromLTRB(16, 10, 16, 0),
                          child: Center(
                            child: ConstrainedBox(
                              constraints: const BoxConstraints(
                                  maxWidth: conversationWidth),
                              child: OfflineNotice(
                                key: const Key('offline-notice'),
                                status: c,
                                onRetry: () =>
                                    unawaited(_chat.retryConnection()),
                                onSettings: () => _go(
                                    WorkspaceDestination.settings,
                                    section: 'connection'),
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
                        onRetry: () => unawaited(_chat.retryConnection()),
                        onSettings: () => _go(WorkspaceDestination.settings,
                            section: 'connection'),
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

  /// Labels routes with their bound provider. When a route is bound off
  /// Ollama, exact models are grouped under "Ollama (direct)": they always
  /// run on Ollama and bypass the binding.
  final ModelRouting routing;
  final ValueChanged<String> onSelected;

  const _ModelPill({
    required this.label,
    required this.models,
    required this.current,
    required this.routing,
    required this.onSelected,
  });

  List<PopupMenuEntry<String>> _items(SonderTokens tokens) {
    PopupMenuItem<String> item(String m, String text) => PopupMenuItem<String>(
          value: m,
          child: Row(children: [
            if (m == current)
              Icon(Icons.check, size: 16, color: tokens.accent)
            else
              const SizedBox(width: 16),
            const SizedBox(width: 10),
            Flexible(child: Text(text, style: tokens.mono(13))),
          ]),
        );
    if (!routing.bypassesBinding) {
      return [for (final m in models) item(m, routing.pickerLabel(m))];
    }
    final exact = [for (final m in models) if (!routing.isRoute(m)) m];
    return [
      for (final m in models)
        if (routing.isRoute(m)) item(m, routing.pickerLabel(m)),
      if (exact.isNotEmpty) ...[
        const PopupMenuDivider(),
        PopupMenuItem<String>(
          key: const Key('model-group-ollama-direct'),
          enabled: false,
          height: 32,
          child: Text(ModelRouting.ollamaDirect,
              style: tokens.mono(11, color: tokens.muted)),
        ),
        for (final m in exact) item(m, m),
      ],
    ];
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return PopupMenuButton<String>(
      tooltip: 'Choose inference route or model',
      onSelected: onSelected,
      position: PopupMenuPosition.under,
      itemBuilder: (_) => _items(tokens),
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
        // Tight phone headers keep the title readable first; the project
        // button returns as soon as there is room for both.
        final roomForProject = constraints.maxWidth >= 200;
        return Row(children: [
          Expanded(
              child: Tooltip(
                  message: '$title / $_count',
                  child: Text(title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: text.titleSmall))),
          if (roomForProject)
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
