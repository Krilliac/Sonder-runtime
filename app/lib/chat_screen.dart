import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

import 'api.dart';
import 'chat_store.dart';
import 'models.dart';
import 'safety_colors.dart';
import 'theme.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'system_screen.dart';

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

class _OpenSystemIntent extends Intent {
  const _OpenSystemIntent();
}

class _CyclePermissionModeIntent extends Intent {
  const _CyclePermissionModeIntent();
}

class ChatScreen extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onSettingsChanged;

  const ChatScreen({
    super.key,
    required this.settings,
    required this.onSettingsChanged,
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  final _messages = <ChatMessage>[];
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _inputFocus = FocusNode();

  // Slash-command palette. Non-empty only while the composer holds a single
  // "/word" token with no space yet, so typing a normal message that merely
  // contains a slash never opens it.
  List<SonderCommand> _paletteMatches = const [];
  int _paletteSelected = 0;

  /// True while the palette is showing the server's "popular" shortlist (the
  /// user has typed a bare "/"), which is the only case that gets category
  /// headers — once a query narrows the list, headers are just noise.
  bool _paletteGrouped = false;

  /// The server's command catalog, fetched once on init and then filtered
  /// entirely in memory. Starts as the hardcoded fallback so the palette works
  /// before the fetch lands, and stays that way if the fetch fails.
  CommandCatalog _catalog = _fallbackCatalog;
  bool _catalogFromServer = false;

  /// Append the original exception to error bubbles. Off by default: the
  /// friendly message is what a person needs, the raw one is what a
  /// maintainer needs. Handled client-side because the errors most worth
  /// reading raw are transport failures, and during one of those the server
  /// cannot answer a command at all.
  bool _verboseErrors = false;
  List<ChatThread> _threads = const [];
  String _currentThreadId = '';
  String _project = 'default';
  bool _sending = false;
  bool _loadingThreads = true;
  Timer? _statusTimer;
  SystemInfo? _systemInfo;

  /// What the agent is allowed to do without asking, as the server reports it.
  ///
  /// Null means "we do not currently know" — an older server without the
  /// route, an unreachable one, or a read that failed — and the indicator is
  /// hidden in that case. It is never left holding the last known value: a
  /// stale mode shown as if it were current is worse than showing nothing,
  /// because the whole point of the chip is knowing what will happen before
  /// you press send.
  PermissionMode? _permissionMode;
  Timer? _permissionModeTimer;
  bool _switchingMode = false;

  // The inference route/model to answer with. "sonder" is the local route;
  // other entries route to a model on the server.
  // Populated from GET /v1/models, with a fallback if the server is unreachable.
  late String _model;
  List<String> _models = const ['sonder'];

  /// Offline fallback for the command palette.
  ///
  /// The real surface lives in the server's `command_catalog.py` and arrives
  /// over GET /v1/commands; this short list is what the palette falls back to
  /// when that fetch fails, so typing "/" offline still offers something
  /// rather than an empty panel.
  static const _quickCommands = <String, String>{
    '/stats': 'Show learning stats',
    '/context': 'Show context health',
    '/compact': 'Preview context compaction',
    '/commands': 'List command registry',
    '/activity': 'Show live tool/file activity',
    '/runtime': 'Show shared local model routing',
    '/mcp': 'Audit live MCP source/tool convergence',
    '/learning': 'Inspect grounded learning and memory quality',
    '/artifactcheck': 'Validate a generated file or artifact pack',
    '/asset office-suite DOCX report, XLSX workbook, PPTX deck':
        'Generate a grounded editable Office suite',
    '/asset media-suite AVI video, animated GIF, MIDI score, SRT WebVTT captions, EDL timeline':
        'Generate a grounded editable media kit',
    '/asset rigged-character textured PBR humanoid GLB with a 17-bone rig, full morph frames, and sequenced Idle Walk Run clips':
        'Generate a grounded animated humanoid character',
    '/autopilot': 'Plan or run a persistent guarded goal',
    '/report': 'Show latest end report and exact actions',
    '/checklist': 'Show the active work checklist',
    '/inventory': 'Summarize the guarded workspace',
    '/privacy': 'Review redacted memory privacy findings',
    '/tree': 'Inspect the guarded workspace tree',
    '/programs python': 'Find the local Python runtime',
    '/dump': 'Save chat/debug dump',
    '/todo': 'Show visible task state',
    '/quality': 'Audit memory quality',
    '/emotion': 'Show or tune tone vectors',
    '/prefer': 'Show or teach preferences',
    '/improve': 'Show next improvements',
    '/agents': 'Show live agent activity',
    '/capacity': 'Show hardware-safe fleet capacity',
    '/agentretry': 'Retry interrupted persisted master work',
    '/forge': 'Build and test the in-house reference game suite',
    '/permissions': 'Show permission rules',
    '/master': 'Choose inline or delegated execution',
    '/runwindow': 'Launch last code in a Windows console',
    '/help': 'List commands',
    '/train': 'Grounded practice; does not update model weights',
    '/pass': 'Mark last answer good',
    '/accept': 'Mark last answer useful',
    '/edited': 'Mark answer used after edits',
    '/fail': 'Mark last answer bad',
  };

  /// Names promoted to the top of the offline palette, mirroring the shape of
  /// the server's own `popular` list.
  static const _fallbackPopular = <String>[
    '/help',
    '/commands',
    '/stats',
    '/context',
    '/todo',
    '/activity',
    '/report',
    '/dump',
  ];

  /// [_quickCommands] rendered as a catalog so the palette has exactly one
  /// code path whether or not the server answered.
  ///
  /// Risk is left blank rather than guessed: an unlabelled row is honest,
  /// a wrongly-green one is not. The `/asset …` entries keep their example
  /// payload in the name, which is what gets inserted into the composer.
  static final CommandCatalog _fallbackCatalog = CommandCatalog(
    commands: _quickCommands.entries
        .map(
          (e) => SonderCommand(
            name: e.key,
            category: 'quick',
            summary: e.value,
            native: true,
            usage: e.key,
          ),
        )
        .toList(growable: false),
    categories: const {'quick': 'Built-in quick commands (offline fallback)'},
    popular: _fallbackPopular,
  );

  SonderApi get _api => SonderApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
      );

  @override
  void initState() {
    super.initState();
    _model = widget.settings.model;
    _loadThreads();
    _refreshModels();
    _refreshCommands();
    _refreshStatus();
    _statusTimer = Timer.periodic(
      const Duration(seconds: 1),
      (_) => _refreshStatus(),
    );
    _refreshPermissionMode();
    // Slower than the status poll on purpose. The mode only changes when
    // somebody changes it — from here, or from a terminal session cycling it
    // with Shift+Tab — so this is a drift check, not a live feed. The paths
    // that matter (app resumed, message sent) refresh it directly.
    _permissionModeTimer = Timer.periodic(
      const Duration(seconds: 15),
      (_) => _refreshPermissionMode(),
    );
    WidgetsBinding.instance.addObserver(this);
    _input.addListener(_updatePalette);
  }

  /// A terminal session can cycle the mode while the app is in the background,
  /// so re-read it the moment the app is in front of the user again.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);
    if (state == AppLifecycleState.resumed) _refreshPermissionMode();
  }

  /// Read the current autonomy mode, or clear it if it cannot be read.
  ///
  /// Any failure — no route on an older server, no server at all, an
  /// unparseable body — clears the chip rather than leaving the previous
  /// value on screen.
  Future<void> _refreshPermissionMode() async {
    PermissionMode? mode;
    try {
      mode = await _api.fetchPermissionMode();
    } catch (_) {
      mode = null;
    }
    if (!mounted) return;
    final next = (mode != null && mode.isUsable) ? mode : null;
    if (next?.mode == _permissionMode?.mode &&
        next?.elevated == _permissionMode?.elevated &&
        next?.elevationReason == _permissionMode?.elevationReason) {
      return; // nothing visible changed; skip the rebuild
    }
    setState(() => _permissionMode = next);
  }

  /// Pick a mode from the four the server publishes, with their blurbs.
  Future<void> _openPermissionModePicker() async {
    final current = _permissionMode;
    if (current == null || _switchingMode) return;
    final picked = await showDialog<String>(
      context: context,
      builder: (_) => _PermissionModeDialog(state: current),
    );
    if (picked == null || !mounted || picked == current.mode) return;

    await _setPermissionMode(picked);
  }

  Future<void> _setPermissionMode(String picked) async {
    if (_switchingMode || _permissionMode == null) return;
    setState(() => _switchingMode = true);
    try {
      final next = await _api.setPermissionMode(picked);
      if (!mounted) return;
      setState(() => _permissionMode = next.isUsable ? next : null);
    } on SonderException catch (e) {
      if (!mounted) return;
      // The switch failed, so what the server holds is now unknown — drop the
      // chip instead of leaving the old label sitting there looking current,
      // and re-read in the background.
      setState(() => _permissionMode = null);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Could not change mode: ${e.message}')),
      );
      unawaited(_refreshPermissionMode());
    } finally {
      if (mounted) setState(() => _switchingMode = false);
    }
  }

  void _cyclePermissionMode() {
    final current = _permissionMode;
    if (current == null || _switchingMode || current.options.isEmpty) return;
    final index = current.options.indexWhere(
      (option) => option.name == current.mode,
    );
    final next = current.options[(index + 1) % current.options.length];
    unawaited(_setPermissionMode(next.name));
  }

  /// Pull the server's command catalog once, and keep the hardcoded fallback
  /// if it cannot be reached.
  ///
  /// Deliberately silent on failure: the palette degrading from ~265 commands
  /// to ~35 is not worth an error bubble mid-conversation, and every command
  /// still works when typed regardless of what the palette knows about.
  Future<void> _refreshCommands() async {
    try {
      final catalog = await _api.fetchCommands();
      if (!mounted || catalog.isEmpty) return;
      setState(() {
        _catalog = catalog;
        _catalogFromServer = true;
        // Cleared so the recompute below always applies: its "nothing visible
        // changed" shortcut compares length and first name, which a fallback
        // and a server list can coincidentally share.
        _paletteMatches = const [];
        _paletteGrouped = false;
      });
      // A catalog that landed while the palette was open would otherwise show
      // stale fallback rows until the next keystroke.
      _updatePalette();
    } catch (_) {
      // Offline / no auth / older server without /v1/commands — keep the
      // static fallback list.
    }
  }

  /// Recompute the slash-command matches for whatever is in the composer.
  ///
  /// Opens only for a lone leading "/token": once a space is typed the user
  /// is writing arguments (or prose that happens to contain a slash), so the
  /// palette gets out of the way rather than hovering over the conversation.
  ///
  /// Filtering runs against the in-memory catalog rather than
  /// GET /v1/commands/complete, so narrowing costs no round trip per keypress.
  void _updatePalette() {
    final text = _input.text;
    List<SonderCommand> matches = const [];
    var grouped = false;
    if (text.startsWith('/') && !text.contains(RegExp(r'[\s\n]'))) {
      final query = text.toLowerCase();
      if (query == '/') {
        // A bare slash is a browse, not a search: show the shortlist the
        // server marks as popular, labelled by category.
        matches = _catalog.popularCommands;
        grouped = true;
      } else {
        matches =
            _catalog.commands.where((c) => c.matchesPrefix(query)).toList();
        // Nothing starts with it -- fall back to matching the summary and
        // category too, so "/memory" still finds the quality and privacy
        // audits.
        if (matches.isEmpty) {
          final needle = query.substring(1);
          matches =
              _catalog.commands.where((c) => c.matchesLoose(needle)).toList();
        }
      }
    }
    if (grouped == _paletteGrouped &&
        matches.length == _paletteMatches.length &&
        (matches.isEmpty || matches.first.name == _paletteMatches.first.name)) {
      return; // nothing visible changed; skip the rebuild
    }
    setState(() {
      _paletteMatches = matches;
      _paletteGrouped = grouped;
      _paletteSelected = 0;
    });
  }

  void _pickCommand(String command) {
    // Commands whose key carries an example payload are inserted whole and
    // left for the user to edit; bare commands get a trailing space so
    // arguments can be typed straight away.
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

  /// Enter sends, Shift+Enter inserts a newline, and palette navigation uses
  /// arrows/Tab/Escape while it is open. Every other key falls through to the
  /// TextField untouched.
  KeyEventResult _onComposerKey(KeyEvent event) {
    if (event is! KeyDownEvent) {
      return KeyEventResult.ignored;
    }
    final key = event.logicalKey;
    if (_paletteMatches.isEmpty && key == LogicalKeyboardKey.enter) {
      if (HardwareKeyboard.instance.isShiftPressed) {
        final value = _input.value;
        final start = value.selection.start < 0
            ? value.text.length
            : value.selection.start;
        final end = value.selection.end < 0 ? start : value.selection.end;
        final text = value.text.replaceRange(start, end, '\n');
        _input.value = value.copyWith(
          text: text,
          selection: TextSelection.collapsed(offset: start + 1),
          composing: TextRange.empty,
        );
      } else {
        _send();
      }
      return KeyEventResult.handled;
    }
    if (_paletteMatches.isEmpty) {
      return KeyEventResult.ignored;
    }
    if (key == LogicalKeyboardKey.arrowDown) {
      setState(
        () =>
            _paletteSelected = (_paletteSelected + 1) % _paletteMatches.length,
      );
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.arrowUp) {
      setState(
        () => _paletteSelected =
            (_paletteSelected - 1 + _paletteMatches.length) %
                _paletteMatches.length,
      );
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

  ChatThread get _currentThread {
    return _threads.firstWhere(
      (t) => t.id == _currentThreadId,
      orElse: () => _threads.isNotEmpty ? _threads.first : ChatThread.fresh(),
    );
  }

  Future<void> _loadThreads() async {
    final threads = await ChatStore.load();
    if (!mounted) return;
    final current = threads.first;
    setState(() {
      _threads = threads;
      _currentThreadId = current.id;
      _project = current.project;
      _messages
        ..clear()
        ..addAll(current.messages);
      _loadingThreads = false;
    });
  }

  Future<void> _saveCurrentThread({
    String? title,
    String? project,
    List<ChatMessage>? messages,
  }) async {
    if (_currentThreadId.isEmpty) return;
    final nextMessages =
        (messages ?? _messages).where((m) => !m.pending).toList();
    final nextTitle = title ?? _titleForMessages(nextMessages);
    final nextProject = (project ?? _project).trim().isEmpty
        ? 'default'
        : (project ?? _project).trim();
    final updated = _threads.map((thread) {
      if (thread.id != _currentThreadId) return thread;
      return thread.copyWith(
        title: nextTitle,
        project: nextProject,
        messages: nextMessages,
        updatedAt: DateTime.now(),
      );
    }).toList()
      ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    setState(() {
      _threads = updated;
      _project = nextProject;
    });
    await ChatStore.save(updated);
  }

  String _titleForMessages(List<ChatMessage> messages) {
    final userMessages = messages.where((m) => m.role == Role.user);
    if (userMessages.isEmpty) return _currentThread.title;
    final text =
        userMessages.first.content.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (text.isEmpty) return 'New chat';
    if (text.length <= 42) return text;
    return '${text.substring(0, 42)}...';
  }

  Future<void> _refreshModels() async {
    try {
      final models = await _api.listModels();
      if (!mounted || models.isEmpty) return;
      setState(() {
        _models = models;
        _model = resolveCatalogModel(_models, _model);
      });
    } catch (_) {
      // Offline / no auth — keep the static fallback list.
    }
  }

  /// Browse the command surface by category.
  ///
  /// A flat menu worked at ~35 commands and does not at ~265, so the toolbar
  /// opens a two-level browser (categories, then the commands inside one)
  /// with a search box across the whole catalog. Picking a command loads it
  /// into the composer rather than sending it, because most commands take
  /// arguments the user still has to fill in.
  Future<void> _openCommandBrowser() async {
    final picked = await showDialog<String>(
      context: context,
      builder: (_) =>
          _CommandBrowser(catalog: _catalog, fromServer: _catalogFromServer),
    );
    if (picked == null || !mounted) return;
    _pickCommand(picked);
  }

  void _selectModel(String m) {
    setState(() => _model = m);
    widget.settings.model = m;
    widget.settings.save();
  }

  Future<void> _refreshStatus() async {
    try {
      final info = await _api.systemInfo();
      if (!mounted) return;
      setState(() => _systemInfo = info);
    } catch (_) {
      if (!mounted) return;
      setState(() => _systemInfo = null);
    }
  }

  Future<void> _recordPassive(String command) async {
    try {
      await _api.chat(
        [ChatMessage(role: Role.user, content: command)],
        model: _model,
        contextSize: widget.settings.contextSize,
        sessionId: _currentThreadId,
        project: _project,
        allowApproximateLocation: widget.settings.allowApproximateLocation,
      );
    } catch (_) {
      // Passive learning should never interrupt the chat UI.
    }
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _permissionModeTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _input.removeListener(_updatePalette);
    _input.dispose();
    _scroll.dispose();
    _inputFocus.dispose();
    super.dispose();
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  /// Client-side settings that must work while the server is unreachable.
  ///
  /// Anything routed to the server is useless during a transport failure,
  /// which is exactly when someone reaches for raw error detail. Returns the
  /// reply to show, or null to let the message go to the server as normal.
  /// Plain-English phrasings are accepted alongside the slash form so the
  /// toggle can be reached by asking rather than by remembering syntax.
  String? _localToggle(String text) {
    final t = text.trim().toLowerCase();

    bool wantsOn(String s) =>
        s.contains(' on') ||
        s.contains('enable') ||
        s.contains('show') ||
        s.contains('turn on');
    bool wantsOff(String s) =>
        s.contains(' off') ||
        s.contains('disable') ||
        s.contains('hide') ||
        s.contains('turn off');

    final isVerboseTopic = t.startsWith('/verbose') ||
        t.startsWith('/errors') ||
        (RegExp(r'\b(raw|verbose|full|detailed)\b').hasMatch(t) &&
            RegExp(r'\b(error|errors|exception|exceptions|traceback)\b')
                .hasMatch(t));
    if (!isVerboseTopic) return null;

    if (wantsOff(t)) {
      setState(() => _verboseErrors = false);
      return 'Verbose errors are **off**. Error messages will show the plain '
          'explanation only.';
    }
    if (wantsOn(t) || t == '/verbose' || t == '/errors') {
      setState(() => _verboseErrors = true);
      return 'Verbose errors are **on**. Failures will show the original '
          'exception under the explanation.\n\nTurn it off with `/verbose off` '
          '(or just ask).';
    }
    return 'Verbose errors are currently **${_verboseErrors ? "on" : "off"}**. '
        'Say `/verbose on` or `/verbose off` — plain English works too.';
  }

  Future<void> _send([String? preset]) async {
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty || _sending) return;

    final localReply = _localToggle(text);
    if (localReply != null) {
      setState(() {
        _messages.add(ChatMessage(role: Role.user, content: text));
        _messages.add(ChatMessage(role: Role.assistant, content: localReply));
        if (preset == null) _input.clear();
      });
      _scrollToEnd();
      unawaited(_saveCurrentThread());
      return;
    }

    setState(() {
      _messages.add(ChatMessage(role: Role.user, content: text));
      _messages.add(
        const ChatMessage(role: Role.assistant, content: '', pending: true),
      );
      _sending = true;
      if (preset == null) _input.clear();
    });
    _scrollToEnd();

    try {
      // Send everything except the trailing pending placeholder.
      final history = _messages.sublist(0, _messages.length - 1);
      final reply = await _api.chatDetailed(
        history,
        model: _model,
        contextSize: widget.settings.contextSize,
        sessionId: _currentThreadId,
        project: _project,
        allowApproximateLocation: widget.settings.allowApproximateLocation,
      );
      setState(() {
        _messages[_messages.length - 1] = ChatMessage(
          role: Role.assistant,
          content: reply.text.isEmpty ? '(empty response)' : reply.text,
          reasoning: reply.reasoning,
          responseMetadata: reply.metadata,
        );
      });
    } on SonderException catch (e) {
      setState(() {
        final diagnostics = <String>[
          if (e.diagnosticText.isNotEmpty) e.diagnosticText,
          if (_verboseErrors && e.cause != null) 'cause: ${e.cause}',
        ];
        _messages[_messages.length - 1] = ChatMessage(
          role: Role.assistant,
          // Diagnostics stay separate from content so they remain available
          // locally without being copied into later model-visible history.
          content: e.message,
          error: true,
          diagnostic: diagnostics.join('\n'),
        );
      });
    } finally {
      if (mounted) {
        await _saveCurrentThread();
        setState(() => _sending = false);
        _refreshStatus();
        // A turn can be the thing that changes the mode (or a terminal session
        // may have changed it mid-turn), so re-read rather than wait out the
        // poll interval.
        _refreshPermissionMode();
        _scrollToEnd();
        _inputFocus.requestFocus();
      }
    }
  }

  void _newChat() {
    final fresh = ChatThread.fresh(project: _project);
    final updated = [fresh, ..._threads];
    setState(() {
      _threads = updated;
      _currentThreadId = fresh.id;
      _project = fresh.project;
      _messages.clear();
    });
    unawaited(ChatStore.save(updated));
  }

  void _switchThread(ChatThread thread) {
    setState(() {
      _currentThreadId = thread.id;
      _project = thread.project;
      _messages
        ..clear()
        ..addAll(thread.messages);
    });
    unawaited(Navigator.of(context).maybePop());
    _scrollToEnd();
  }

  Future<void> _deleteThread(ChatThread thread) async {
    final remaining = _threads.where((t) => t.id != thread.id).toList();
    final next =
        remaining.isEmpty ? [ChatThread.fresh(project: _project)] : remaining;
    final current = thread.id == _currentThreadId ? next.first : _currentThread;
    setState(() {
      _threads = next;
      _currentThreadId = current.id;
      _project = current.project;
      _messages
        ..clear()
        ..addAll(current.messages);
    });
    await ChatStore.save(next);
  }

  Future<void> _editProject() async {
    final controller = TextEditingController(text: _project);
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
    await _saveCurrentThread(project: value);
  }

  Future<void> _openSettings() async {
    await Navigator.of(context).push(
      PageRouteBuilder<void>(
        transitionDuration: Duration.zero,
        reverseTransitionDuration: Duration.zero,
        pageBuilder: (_, __, ___) => SettingsScreen(
          settings: widget.settings,
          onChanged: widget.onSettingsChanged,
        ),
        transitionsBuilder: (_, __, ___, child) => child,
      ),
    );
    setState(() {
      // Pick up server/key/model changes; re-fetch the model list if it moved.
      _model = widget.settings.model;
    });
    _refreshModels();
    // A different server URL or key means a different mode entirely.
    _refreshPermissionMode();
  }

  Future<void> _openSystem() async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => SystemScreen(settings: widget.settings),
      ),
    );
  }

  Future<void> _openThreadSwitcher(bool desktop) async {
    if (!desktop) {
      await showModalBottomSheet<void>(
        context: context,
        builder: (_) => SizedBox(
          height: 560,
          child: _ChatDrawer(
            threads: _threads,
            currentThreadId: _currentThreadId,
            onNew: _newChat,
            onSelect: _switchThread,
            onDelete: _deleteThread,
          ),
        ),
      );
      return;
    }
    await showDialog<void>(
      context: context,
      builder: (_) => Dialog(
        child: SizedBox(
          width: 360,
          height: 600,
          child: _ChatDrawer(
            threads: _threads,
            currentThreadId: _currentThreadId,
            onNew: _newChat,
            onSelect: _switchThread,
            onDelete: _deleteThread,
          ),
        ),
      ),
    );
  }

  String _modelLabel(String m) => m == 'sonder' ? 'sonder (local route)' : m;

  @override
  Widget build(BuildContext context) {
    final currentTitle =
        _loadingThreads ? 'Loading chats...' : _currentThread.displayTitle;
    return LayoutBuilder(
      builder: (context, constraints) {
        // Keep the drawer gesture-first on phones, but make the conversation
        // workspace feel like a real desktop app once there is room for a
        // persistent chat rail.  The same widget and callbacks are used in
        // both modes, so thread selection has one source of truth.
        final desktop = constraints.maxWidth >= 1000;
        final drawer = _ChatDrawer(
          threads: _threads,
          currentThreadId: _currentThreadId,
          onNew: _newChat,
          onSelect: _switchThread,
          onDelete: _deleteThread,
          embedded: desktop,
          serverUrl: widget.settings.serverUrl,
          connected: _systemInfo != null,
          onOpenCommands: desktop ? _openCommandBrowser : null,
          onOpenSystem: desktop ? _openSystem : null,
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
                _OpenSystemIntent(),
            SingleActivator(LogicalKeyboardKey.tab, shift: true):
                _CyclePermissionModeIntent(),
          },
          child: Actions(
            actions: <Type, Action<Intent>>{
              _OpenCommandBrowserIntent:
                  CallbackAction<_OpenCommandBrowserIntent>(
                onInvoke: (_) {
                  unawaited(_openCommandBrowser());
                  return null;
                },
              ),
              _NewChatIntent: CallbackAction<_NewChatIntent>(
                onInvoke: (_) {
                  _newChat();
                  return null;
                },
              ),
              _OpenThreadSwitcherIntent:
                  CallbackAction<_OpenThreadSwitcherIntent>(
                onInvoke: (_) {
                  unawaited(_openThreadSwitcher(desktop));
                  return null;
                },
              ),
              _OpenSettingsIntent: CallbackAction<_OpenSettingsIntent>(
                onInvoke: (_) {
                  unawaited(_openSettings());
                  return null;
                },
              ),
              _OpenSystemIntent: CallbackAction<_OpenSystemIntent>(
                onInvoke: (_) {
                  unawaited(_openSystem());
                  return null;
                },
              ),
              _CyclePermissionModeIntent:
                  CallbackAction<_CyclePermissionModeIntent>(
                onInvoke: (_) {
                  _cyclePermissionMode();
                  return null;
                },
              ),
            },
            child: Scaffold(
              drawer: desktop ? null : drawer,
              appBar: AppBar(
                titleSpacing: desktop ? 20 : 0,
                title: _ChatHeader(
                  title: currentTitle,
                  project: _project,
                  messageCount: _messages.where((m) => !m.pending).length,
                  onEditProject: _editProject,
                ),
                actions: [
                  // Model picker: switch which LLM answers, per conversation.
                  _ModelPill(
                    label: _modelLabel(_model),
                    models: _models,
                    current: _model,
                    labelFor: _modelLabel,
                    onSelected: _selectModel,
                  ),
                  if (!desktop) ...[
                    const SizedBox(width: 4),
                    IconButton(
                      tooltip: 'Commands',
                      icon: const Icon(Icons.bolt_outlined),
                      onPressed: _openCommandBrowser,
                    ),
                    IconButton(
                      tooltip: 'New chat',
                      icon: const Icon(Icons.add_comment_outlined),
                      onPressed: _newChat,
                    ),
                    IconButton(
                      tooltip: 'System',
                      icon: const Icon(Icons.dashboard_customize_outlined),
                      onPressed: _openSystem,
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
                        Expanded(
                          child: _messages.isEmpty
                              ? _EmptyState(
                                  serverUrl: widget.settings.serverUrl,
                                  onQuick: _send,
                                )
                              : ListView.builder(
                                  controller: _scroll,
                                  padding: const EdgeInsets.fromLTRB(
                                    16,
                                    24,
                                    16,
                                    16,
                                  ),
                                  itemCount: _messages.length,
                                  itemBuilder: (_, i) => Center(
                                    child: ConstrainedBox(
                                      constraints: const BoxConstraints(
                                        maxWidth: 760,
                                      ),
                                      child: _Turn(
                                        message: _messages[i],
                                        onPassive: _recordPassive,
                                      ),
                                    ),
                                  ),
                                ),
                        ),
                        _InputBar(
                          controller: _input,
                          focusNode: _inputFocus,
                          sending: _sending,
                          onSend: () => _send(),
                          paletteMatches: _paletteMatches,
                          paletteSelected: _paletteSelected,
                          paletteGrouped: _paletteGrouped,
                          paletteCategories: _catalog.categories,
                          onPalettePick: _pickCommand,
                          onKey: _onComposerKey,
                          permissionMode: _permissionMode,
                          permissionModeBusy: _switchingMode,
                          onTapPermissionMode: _openPermissionModePicker,
                          onOpenCommands: _openCommandBrowser,
                          desktop: desktop,
                        ),
                        _LiveStatusBar(
                          info: _systemInfo,
                          model: _model,
                          permissionMode: _permissionMode,
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

class _EmptyState extends StatelessWidget {
  final String serverUrl;
  final ValueChanged<String> onQuick;
  const _EmptyState({required this.serverUrl, required this.onQuick});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(
      builder: (context, constraints) {
        final minHeight =
            constraints.maxHeight > 64 ? constraints.maxHeight - 64 : 0.0;
        return SingleChildScrollView(
          padding: const EdgeInsets.all(32),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: minHeight),
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const _Mark(size: 40),
                    const SizedBox(height: 20),
                    Text('Sonder Runtime', style: text.headlineSmall),
                    const SizedBox(height: 8),
                    Text(
                      'Not a standalone model: Sonder Runtime supplies routing, '
                      'prompts, memory, tools, and policy to model weights '
                      'served locally by Ollama.',
                      style: text.bodyMedium?.copyWith(color: tokens.text2),
                    ),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Container(
                          width: 6,
                          height: 6,
                          decoration: BoxDecoration(
                            color: tokens.ok,
                            borderRadius: BorderRadius.circular(3),
                          ),
                        ),
                        const SizedBox(width: 8),
                        Flexible(
                          child: Text(
                            'Connected to $serverUrl',
                            style: tokens.mono(12, color: tokens.muted),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 28),
                    Text('Try', style: text.labelSmall),
                    const SizedBox(height: 10),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        _Suggestion(
                          'Write a Python function to parse a CSV',
                          onQuick,
                        ),
                        _Suggestion('Explain async/await simply', onQuick),
                        _Suggestion('/stats', onQuick),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}


/// The product mark: the signal tile with a stroked hexagon, sized by role.
class _Mark extends StatelessWidget {
  final double size;
  const _Mark({this.size = 22});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: tokens.accentDim,
        borderRadius: BorderRadius.circular(size * 0.28),
      ),
      child: Icon(
        Icons.hexagon_outlined,
        size: size * 0.6,
        color: tokens.accent,
      ),
    );
  }
}


/// The model picker as a quiet pill: the route name in the transcript face,
/// a chevron, and the menu of everything the server offers.
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
          .map(
            (m) => PopupMenuItem<String>(
              value: m,
              child: Row(
                children: [
                  if (m == current)
                    Icon(Icons.check, size: 16, color: tokens.accent)
                  else
                    const SizedBox(width: 16),
                  const SizedBox(width: 10),
                  Text(labelFor(m), style: tokens.mono(13)),
                ],
              ),
            ),
          )
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
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: Text(
                label,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(12, weight: FontWeight.w500),
              ),
            ),
            const SizedBox(width: 4),
            Icon(Icons.expand_more, size: 16, color: tokens.muted),
          ],
        ),
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

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Row(
      children: [
        Flexible(
          child: Text(
            title,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: text.titleSmall,
          ),
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
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
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
                ],
              ),
            ),
          ),
        ),
        const SizedBox(width: 10),
        Text(
          '$messageCount messages',
          style: tokens.mono(11, color: tokens.muted),
        ),
      ],
    );
  }
}

class _ChatDrawer extends StatelessWidget {
  final List<ChatThread> threads;
  final String currentThreadId;
  final VoidCallback onNew;
  final ValueChanged<ChatThread> onSelect;
  final ValueChanged<ChatThread> onDelete;
  final bool embedded;
  final String serverUrl;
  final bool connected;
  final VoidCallback? onOpenCommands;
  final VoidCallback? onOpenSystem;
  final VoidCallback? onOpenSettings;

  const _ChatDrawer({
    required this.threads,
    required this.currentThreadId,
    required this.onNew,
    required this.onSelect,
    required this.onDelete,
    this.embedded = false,
    this.serverUrl = '',
    this.connected = false,
    this.onOpenCommands,
    this.onOpenSystem,
    this.onOpenSettings,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final projects = threads.map((t) => t.project).toSet().toList()..sort();
    final endpoint = serverUrl
        .replaceFirst(RegExp(r'^https?://'), '')
        .replaceAll(RegExp(r'/+$'), '');
    return Drawer(
      shape: embedded
          ? Border(right: BorderSide(color: tokens.hairline))
          : null,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (embedded)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 11, 8, 4),
                child: Row(
                  children: [
                    const _Mark(),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            'Sonder',
                            style: text.titleSmall?.copyWith(
                              fontWeight: FontWeight.w600,
                              height: 1.1,
                            ),
                          ),
                          Text(
                            'Local-first workspace',
                            style: tokens.mono(10, color: tokens.muted),
                          ),
                        ],
                      ),
                    ),
                    IconButton(
                      tooltip: 'New chat',
                      onPressed: onNew,
                      icon: const Icon(Icons.add, size: 18),
                    ),
                  ],
                ),
              )
            else
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 8, 4),
                child: Row(
                  children: [
                    Expanded(child: Text('Chats', style: text.titleSmall)),
                    IconButton(
                      tooltip: 'New chat',
                      onPressed: () {
                        unawaited(Navigator.of(context).maybePop());
                        onNew();
                      },
                      icon: const Icon(Icons.add_comment_outlined, size: 18),
                    ),
                  ],
                ),
              ),
            if (embedded)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 2),
                child: Text('Chats', style: text.labelSmall),
              ),
            Expanded(
              child: threads.isEmpty
                  ? Center(
                      child: Text(
                        'No chats yet',
                        style: text.bodySmall?.copyWith(color: tokens.muted),
                      ),
                    )
                  : ListView.builder(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 8,
                        vertical: 4,
                      ),
                      itemCount: threads.length,
                      itemBuilder: (_, index) {
                        final thread = threads[index];
                        final selected = thread.id == currentThreadId;
                        return _ThreadRow(
                          thread: thread,
                          selected: selected,
                          onTap: () => onSelect(thread),
                          onDelete:
                              threads.length <= 1 ? null : () => onDelete(thread),
                        );
                      },
                    ),
            ),
            if (projects.isNotEmpty) ...[
              Divider(height: 1, color: tokens.hairline),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 4),
                child: Text('Projects', style: text.labelSmall),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final project in projects.take(4))
                      Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 8,
                          vertical: 5,
                        ),
                        child: Row(
                          children: [
                            Container(
                              width: 7,
                              height: 7,
                              decoration: BoxDecoration(
                                color: tokens.hairlineStrong,
                                borderRadius: BorderRadius.circular(4),
                              ),
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Text(
                                project,
                                overflow: TextOverflow.ellipsis,
                                style: text.bodySmall
                                    ?.copyWith(color: tokens.text2),
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
            ],
            if (embedded) ...[
              Divider(height: 1, color: tokens.hairline),
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 6, 12, 8),
                child: Row(
                  children: [
                    IconButton(
                      tooltip: 'System',
                      onPressed: onOpenSystem,
                      icon: const Icon(Icons.dashboard_customize_outlined),
                    ),
                    IconButton(
                      tooltip: 'Commands',
                      onPressed: onOpenCommands,
                      icon: const Icon(Icons.bolt_outlined),
                    ),
                    IconButton(
                      tooltip: 'Settings',
                      onPressed: onOpenSettings,
                      icon: const Icon(Icons.settings_outlined),
                    ),
                    const Spacer(),
                    Flexible(
                      child: Text(
                        endpoint,
                        overflow: TextOverflow.ellipsis,
                        style: tokens.mono(11, color: tokens.muted),
                      ),
                    ),
                    const SizedBox(width: 6),
                    Container(
                      width: 7,
                      height: 7,
                      decoration: BoxDecoration(
                        color: connected ? tokens.ok : tokens.hairlineStrong,
                        borderRadius: BorderRadius.circular(4),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}


/// One conversation in the rail: its title, and its turn count set in the
/// transcript face. Delete stays behind a real button with a label.
class _ThreadRow extends StatelessWidget {
  final ChatThread thread;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback? onDelete;

  const _ThreadRow({
    required this.thread,
    required this.selected,
    required this.onTap,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Semantics(
      selected: selected,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        child: Container(
          height: 40,
          padding: const EdgeInsets.only(left: 10, right: 2),
          decoration: BoxDecoration(
            color: selected ? tokens.raised : null,
            borderRadius: BorderRadius.circular(SonderRadius.row),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  thread.displayTitle,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: text.bodySmall?.copyWith(
                    fontSize: 13,
                    color: selected ? tokens.text : tokens.text2,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '${thread.messages.length}',
                style: tokens.mono(11, color: tokens.muted),
              ),
              IconButton(
                tooltip: 'Delete chat',
                visualDensity: VisualDensity.compact,
                onPressed: onDelete,
                icon: Icon(Icons.close, size: 14, color: tokens.muted),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Suggestion extends StatelessWidget {
  final String text;
  final ValueChanged<String> onQuick;
  const _Suggestion(this.text, this.onQuick);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ActionChip(
      label: Text(
        text,
        style: text.startsWith('/')
            ? tokens.mono(12)
            : Theme.of(context).textTheme.labelLarge,
      ),
      onPressed: () => onQuick(text),
    );
  }
}

class _LiveStatusBar extends StatelessWidget {
  final SystemInfo? info;
  final String model;
  final PermissionMode? permissionMode;

  const _LiveStatusBar({
    required this.info,
    required this.model,
    this.permissionMode,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final contextInfo = info?.context;
    final agentInfo = info?.agents;
    final activityInfo = info?.activity;
    final responseInfo = activityInfo?.displayResponse;
    final contextText = contextInfo == null
        ? '—'
        : '${contextInfo.contextPercent.toStringAsFixed(1)}% · '
            '${contextInfo.nativeContextLimit} native';
    final routeText = model.trim().isEmpty ? '—' : model;
    final turnTokens = agentInfo == null
        ? '—'
        : '${agentInfo.tokensIn}/${agentInfo.tokensOut}';
    final turnText = responseInfo == null
        ? null
        : '+${responseInfo.linesAdded} ~${responseInfo.linesEdited} '
            '−${responseInfo.linesDeleted} · '
            '$turnTokens tok';
    final segments = <_StatusMetric>[
      _StatusMetric('Context', contextText),
      _StatusMetric('Activity', info?.executionSummary ?? '—'),
      _StatusMetric('Route', routeText),
      if (turnText != null) _StatusMetric('Turn', turnText),
    ];
    final mode = permissionMode;
    return Container(
      width: double.infinity,
      height: 28,
      decoration: BoxDecoration(
        color: tokens.panel,
        border: Border(top: BorderSide(color: tokens.hairline)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Row(
        children: [
          Expanded(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (var i = 0; i < segments.length; i++) ...[
                    if (i > 0) const SizedBox(width: 16),
                    _StatusMetricView(metric: segments[i]),
                  ],
                ],
              ),
            ),
          ),
          if (mode != null) ...[
            const SizedBox(width: 16),
            Text('mode ', style: tokens.mono(11, color: tokens.muted)),
            Text(
              mode.displayLabel,
              style: tokens.mono(
                11,
                color: permissionModeColor(
                  Theme.of(context).colorScheme,
                  mode.mode,
                ),
                weight: FontWeight.w500,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _StatusMetric {
  final String label;
  final String value;

  const _StatusMetric(this.label, this.value);
}

class _StatusMetricView extends StatelessWidget {
  final _StatusMetric metric;

  const _StatusMetricView({required this.metric});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Semantics(
      key: ValueKey('status-metric-${metric.label.toLowerCase()}'),
      label: '${metric.label}: ${metric.value}',
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            metric.label.toLowerCase(),
            style: tokens.mono(11, color: tokens.muted),
          ),
          const SizedBox(width: 5),
          Text(
            metric.value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: tokens.mono(11, color: tokens.text),
          ),
        ],
      ),
    );
  }
}

/// One turn of the transcript: a glyph in the gutter says who is speaking
/// (❯ you, ◈ Sonder, ⊘ a refusal or failure), the content sits in the
/// reading column, and the response's actions and details follow it.
class _Turn extends StatelessWidget {
  final ChatMessage message;
  final ValueChanged<String>? onPassive;
  const _Turn({required this.message, this.onPassive});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final isUser = message.role == Role.user;
    final glyph = isUser ? '❯' : (message.error ? '⊘' : '◈');
    final glyphColor = message.error && !isUser ? tokens.danger : tokens.accent;
    final speaker = isUser ? 'You' : 'Sonder Runtime';

    Widget content;
    if (message.pending) {
      content = Semantics(
        liveRegion: true,
        label: 'Sonder Runtime is working',
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(height: 18, width: 40, child: _TypingDots()),
            const SizedBox(width: 8),
            Text(
              'Working...',
              style: tokens.mono(12, color: tokens.muted),
            ),
          ],
        ),
      );
    } else if (isUser) {
      content = SelectableText(
        message.content,
        style: Theme.of(context).textTheme.bodyMedium,
      );
    } else {
      content = _AssistantContent(
        content: message.content,
        color: tokens.text,
        reasoning: message.reasoning,
        metadata: message.responseMetadata,
        diagnostic: message.diagnostic,
        error: message.error,
      );
    }

    final actions = !isUser && !message.pending && message.content.isNotEmpty;
    return Semantics(
      label: speaker,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 24,
              child: Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text(
                  glyph,
                  textAlign: TextAlign.center,
                  style: tokens.mono(
                    14,
                    color: glyphColor,
                    weight: FontWeight.w600,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.error && !isUser && !message.pending)
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
                      decoration: BoxDecoration(
                        color: tokens.panel,
                        borderRadius: BorderRadius.circular(SonderRadius.row),
                        border: Border.all(color: tokens.hairline),
                      ),
                      child: content,
                    )
                  else
                    content,
                  if (actions)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Wrap(
                        spacing: 6,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          _FeedbackAction(
                            icon: Icons.copy_all_outlined,
                            label: 'Copy response',
                            text: 'copy',
                            onTap: () {
                              Clipboard.setData(
                                ClipboardData(text: message.content),
                              );
                              onPassive?.call('/copied');
                            },
                          ),
                          // Quality feedback trains the learning loop on the
                          // last answer. An error is a transport or server
                          // failure, not an answer, so rating it would teach
                          // the loop about a turn the model never produced.
                          // Copy stays: copying the failure text is exactly
                          // what you want to do with it.
                          if (!message.error) ...[
                            _FeedbackAction(
                              icon: Icons.check_circle_outline,
                              label: 'Mark response useful',
                              text: 'useful',
                              onTap: () => onPassive?.call('/accept'),
                            ),
                            _FeedbackAction(
                              icon: Icons.edit_outlined,
                              label: 'Mark response edited',
                              text: 'edited',
                              onTap: () => onPassive?.call('/edited'),
                            ),
                          ],
                        ],
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// A feedback control with a full-size touch target and an explicit label.
/// The visible text stays compact, while keyboard and assistive technology get
/// a stable action name instead of relying on a 14px icon or hover tooltip.
class _FeedbackAction extends StatelessWidget {
  final IconData icon;
  final String label;
  final String text;
  final VoidCallback onTap;

  const _FeedbackAction({
    required this.icon,
    required this.label,
    required this.text,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Semantics(
      button: true,
      label: label,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 44, minWidth: 44),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(icon, size: 14, color: tokens.muted),
                const SizedBox(width: 5),
                Text(text, style: tokens.mono(11, color: tokens.muted)),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _AssistantContent extends StatelessWidget {
  static const _activityMarker = '=== ACTIVITY (observable work) ===';

  final String content;
  final Color color;
  final String reasoning;
  final ChatResponseMetadata? metadata;
  final String diagnostic;
  final bool error;

  const _AssistantContent({
    required this.content,
    required this.color,
    this.reasoning = '',
    this.metadata,
    this.diagnostic = '',
    this.error = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final markerIndex = content.indexOf(_activityMarker);
    final answer =
        (markerIndex < 0 ? content : content.substring(0, markerIndex))
            .trimRight();
    final activity =
        markerIndex < 0 ? '' : content.substring(markerIndex).trim();
    final body = Theme.of(context).textTheme.bodyMedium?.copyWith(
          color: error ? tokens.text : color,
        );
    final markdownStyle =
        MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
      p: body,
      strong: body?.copyWith(fontWeight: FontWeight.w600),
      h1: Theme.of(context).textTheme.titleLarge,
      h2: Theme.of(context).textTheme.titleMedium,
      h3: Theme.of(context).textTheme.titleSmall,
      a: body?.copyWith(
        color: tokens.accent,
        decoration: TextDecoration.underline,
        decorationColor: tokens.accent.withValues(alpha: 0.5),
      ),
      code: tokens.mono(13, color: tokens.text).copyWith(
        backgroundColor: tokens.raised,
      ),
      codeblockPadding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
      codeblockDecoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: tokens.hairline),
      ),
      blockquoteDecoration: BoxDecoration(
        border: Border(left: BorderSide(color: tokens.hairlineStrong, width: 2)),
      ),
      blockquotePadding: const EdgeInsets.fromLTRB(14, 2, 0, 2),
      horizontalRuleDecoration: BoxDecoration(
        border: Border(top: BorderSide(color: tokens.hairline)),
      ),
      blockSpacing: 10,
      listIndent: 22,
    );

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        MarkdownBody(
          data: answer,
          selectable: true,
          softLineBreak: true,
          styleSheet: markdownStyle,
        ),
        if (reasoning.trim().isNotEmpty) ...[
          const SizedBox(height: 8),
          _CollapsedDetail(
            icon: Icons.psychology_outlined,
            label: 'Model reasoning',
            body: reasoning.trim(),
          ),
        ],
        if (activity.isNotEmpty) ...[
          const SizedBox(height: 8),
          _CollapsedDetail(
            icon: Icons.monitor_heart_outlined,
            label: 'Activity evidence',
            body: activity,
          ),
        ],
        if (metadata != null && metadata!.diagnosticText.isNotEmpty) ...[
          const SizedBox(height: 8),
          _CollapsedDetail(
            icon: Icons.receipt_long_outlined,
            label: metadata!.cache == 'hit'
                ? 'Response details - cached replay'
                : 'Response details',
            body: metadata!.diagnosticText,
          ),
        ],
        if (diagnostic.trim().isNotEmpty) ...[
          const SizedBox(height: 8),
          _CollapsedDetail(
            icon: Icons.error_outline,
            label: 'Error details',
            body: diagnostic.trim(),
          ),
        ],
      ],
    );
  }
}

/// A collapsed, monospaced detail block under an answer.
///
/// Shared by the reasoning, activity, receipt and error sections so they stay
/// visually identical as any one changes: a hairline row that opens into a
/// transcript-face panel.
class _CollapsedDetail extends StatelessWidget {
  final IconData icon;
  final String label;
  final String body;

  const _CollapsedDetail({
    required this.icon,
    required this.label,
    required this.body,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Material(
      color: Colors.transparent,
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          dense: true,
          visualDensity: VisualDensity.compact,
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          leading: Icon(icon, size: 16, color: tokens.muted),
          title: Text(
            label,
            style: tokens.mono(11, color: tokens.text2),
          ),
          children: [
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: tokens.panel,
                borderRadius: BorderRadius.circular(SonderRadius.row),
                border: Border.all(color: tokens.hairline),
              ),
              child: SelectableText(
                body,
                style: tokens.mono(11, color: tokens.text2),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _TypingDots extends StatefulWidget {
  const _TypingDots();
  @override
  State<_TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<_TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  )..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = Theme.of(context).colorScheme.onSurface;
    return AnimatedBuilder(
      animation: _c,
      builder: (_, __) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (i) {
            final t = (_c.value + i * 0.2) % 1.0;
            final opacity = 0.3 + 0.7 * (1 - (t - 0.5).abs() * 2).clamp(0, 1);
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2),
              child: Opacity(
                opacity: opacity.toDouble(),
                child: CircleAvatar(radius: 3, backgroundColor: color),
              ),
            );
          }),
        );
      },
    );
  }
}

/// Small coloured dot that carries a command's risk band.
class _RiskDot extends StatelessWidget {
  final String risk;
  const _RiskDot({required this.risk});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final label = riskLabel(risk);
    return Tooltip(
      message: label,
      child: Semantics(
        label: 'Risk: $label',
        container: true,
        child: SizedBox(
          width: 24,
          height: 24,
          child: Center(
            child: Container(
              width: 9,
              height: 9,
              decoration: BoxDecoration(
                color: riskColor(cs, risk),
                shape: BoxShape.circle,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Compact category tag shown beside a command name.
class _CategoryTag extends StatelessWidget {
  final String category;
  const _CategoryTag({required this.category});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: Text(
        category,
        style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant),
      ),
    );
  }
}

/// One command as it appears in the palette and in the browser: risk dot,
/// name, category tag, summary, and the usage line that says what arguments
/// it takes.
class _CommandRow extends StatelessWidget {
  final SonderCommand command;
  final bool selected;
  final VoidCallback onTap;

  const _CommandRow({
    required this.command,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final usage = command.usageLine;
    final aliases =
        command.aliases.where((alias) => alias.isNotEmpty).join(', ');
    final semanticParts = <String>[
      command.displayName,
      if (command.summary.isNotEmpty) command.summary,
      if (command.category.isNotEmpty) 'category ${command.category}',
      if (command.risk.isNotEmpty) 'risk ${command.risk}',
      if (aliases.isNotEmpty) 'aliases $aliases',
      'usage $usage',
    ];
    return Semantics(
      button: true,
      selected: selected,
      label: semanticParts.join('. '),
      child: InkWell(
        onTap: onTap,
        child: Container(
          color: selected ? cs.primary.withValues(alpha: 0.16) : null,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  _RiskDot(risk: command.risk),
                  const SizedBox(width: 8),
                  SizedBox(
                    width: 150,
                    child: Text(
                      // The asset commands carry a whole example payload as
                      // their name; showing the first token keeps rows readable.
                      command.displayName,
                      style: TextStyle(
                        fontFamily: SonderTheme.mono,
                        fontWeight:
                            selected ? FontWeight.w700 : FontWeight.w500,
                        color: cs.primary,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  if (command.category.isNotEmpty) ...[
                    const SizedBox(width: 8),
                    _CategoryTag(category: command.category),
                  ],
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      command.summary,
                      style: TextStyle(color: cs.onSurfaceVariant),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
              if (usage != command.displayName || aliases.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(left: 17, top: 2),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (usage != command.displayName)
                        Text(
                          usage,
                          style: TextStyle(
                            fontFamily: SonderTheme.mono,
                            fontSize: 11,
                            color: cs.onSurfaceVariant.withValues(alpha: 0.75),
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                      if (aliases.isNotEmpty)
                        Text(
                          'aliases: $aliases',
                          style: TextStyle(
                            fontFamily: SonderTheme.mono,
                            fontSize: 11,
                            color: cs.onSurfaceVariant.withValues(alpha: 0.75),
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// A palette line: either a category heading or a selectable command.
class _PaletteRow {
  final String? heading;
  final SonderCommand? command;

  /// Index into the flat match list, so keyboard selection stays independent
  /// of however many headings were interleaved.
  final int matchIndex;

  const _PaletteRow.heading(this.heading)
      : command = null,
        matchIndex = -1;
  const _PaletteRow.command(this.command, this.matchIndex) : heading = null;
}

/// Command palette that opens when the composer starts with "/".
///
/// Rows come from the server's command catalog (GET /v1/commands), cached in
/// the chat screen and filtered in memory as you type — the same way the
/// slash menu works in the terminal REPL. A bare "/" browses the popular
/// shortlist grouped under category headings; any further character narrows
/// to a flat ranked list where headings would only be noise.
class _CommandPalette extends StatelessWidget {
  final List<SonderCommand> matches;
  final int selected;

  /// Insert category headings (true only for the bare-"/" browse).
  final bool grouped;

  /// Category key -> blurb, used to caption the headings.
  final Map<String, String> categories;
  final ValueChanged<String> onPick;

  const _CommandPalette({
    required this.matches,
    required this.selected,
    required this.grouped,
    required this.categories,
    required this.onPick,
  });

  List<_PaletteRow> get _rows {
    final rows = <_PaletteRow>[];
    String? lastCategory;
    for (var i = 0; i < matches.length; i++) {
      final command = matches[i];
      if (grouped && command.category != lastCategory) {
        lastCategory = command.category;
        rows.add(_PaletteRow.heading(command.category));
      }
      rows.add(_PaletteRow.command(command, i));
    }
    return rows;
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final rows = _rows;
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      constraints: const BoxConstraints(maxHeight: 320),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: ListView.builder(
        key: const Key('command-palette'),
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 4),
        itemCount: rows.length,
        itemBuilder: (context, i) {
          final row = rows[i];
          final command = row.command;
          if (command == null) {
            final key = row.heading ?? '';
            final blurb = categories[key] ?? '';
            return Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 2),
              child: Text(
                blurb.isEmpty
                    ? key.toUpperCase()
                    : '${key.toUpperCase()} — $blurb',
                style: TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 0.6,
                  color: cs.onSurfaceVariant,
                ),
                overflow: TextOverflow.ellipsis,
              ),
            );
          }
          return _CommandRow(
            command: command,
            selected: row.matchIndex == selected,
            onTap: () => onPick(command.name),
          );
        },
      ),
    );
  }
}

/// Two-level browser for the whole command surface, opened from the toolbar.
///
/// The old toolbar menu listed every command flat, which stops working once
/// the catalog is a few hundred entries: this opens on the category list,
/// drills into one category, and offers a search box that cuts across all of
/// them. Returns the picked command's name via [Navigator.pop] so the caller
/// can load it into the composer.
class _CommandBrowser extends StatefulWidget {
  final CommandCatalog catalog;

  /// False when the catalog fetch failed and these are the built-in fallback
  /// commands — surfaced in the footer so a short list is never mistaken for
  /// the real surface.
  final bool fromServer;

  const _CommandBrowser({required this.catalog, required this.fromServer});

  @override
  State<_CommandBrowser> createState() => _CommandBrowserState();
}

class _CommandBrowserState extends State<_CommandBrowser> {
  final _search = TextEditingController();

  /// Null while showing the category list; otherwise the category being
  /// drilled into.
  String? _category;
  String _query = '';

  @override
  void initState() {
    super.initState();
    _search.addListener(() {
      final next = _search.text.trim().toLowerCase();
      if (next == _query) return;
      setState(() => _query = next);
    });
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  /// Search wins over the category drill-down: a query always searches the
  /// whole catalog, so a user never gets an empty result that is really just
  /// "not in this category".
  List<SonderCommand> get _results {
    if (_query.isNotEmpty) {
      final needle = _query.startsWith('/') ? _query.substring(1) : _query;
      return widget.catalog.commands
          .where((c) => c.matchesLoose(needle))
          .toList(growable: false);
    }
    final category = _category;
    if (category == null) return const [];
    return widget.catalog.byCategory[category] ?? const [];
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final grouped = widget.catalog.byCategory;
    final showingCategories = _query.isEmpty && _category == null;
    final results = _results;
    final total = widget.catalog.commands.length;

    return Dialog(
      key: const Key('command-browser'),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760, maxHeight: 620),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  if (!showingCategories)
                    IconButton(
                      tooltip: 'All categories',
                      icon: const Icon(Icons.arrow_back),
                      onPressed: () {
                        _search.clear();
                        setState(() {
                          _category = null;
                          _query = '';
                        });
                      },
                    ),
                  Expanded(
                    child: Text(
                      showingCategories
                          ? 'Commands'
                          : (_query.isNotEmpty
                              ? 'Search results'
                              : _category ?? 'Commands'),
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close',
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
              TextField(
                key: const Key('command-browser-search'),
                controller: _search,
                decoration: const InputDecoration(
                  prefixIcon: Icon(Icons.search),
                  hintText: 'Search all commands…',
                  isDense: true,
                ),
              ),
              const SizedBox(height: 10),
              Flexible(
                child: showingCategories
                    ? ListView(
                        key: const Key('command-browser-categories'),
                        shrinkWrap: true,
                        children: [
                          for (final entry in grouped.entries)
                            ListTile(
                              key: Key('command-category-${entry.key}'),
                              dense: true,
                              leading: const Icon(Icons.folder_outlined),
                              title: Text(entry.key),
                              subtitle: Text(
                                widget.catalog.categories[entry.key] ?? '',
                                overflow: TextOverflow.ellipsis,
                              ),
                              trailing: Text('${entry.value.length}'),
                              onTap: () =>
                                  setState(() => _category = entry.key),
                            ),
                        ],
                      )
                    : ListView.builder(
                        key: const Key('command-browser-commands'),
                        shrinkWrap: true,
                        itemCount: results.length,
                        itemBuilder: (context, i) => _CommandRow(
                          command: results[i],
                          selected: false,
                          onTap: () =>
                              Navigator.of(context).pop(results[i].name),
                        ),
                      ),
              ),
              const SizedBox(height: 8),
              Text(
                widget.fromServer
                    ? '$total commands published by this server.'
                    : 'Server catalog unavailable — showing $total built-in '
                        'commands.',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _InputBar extends StatelessWidget {
  final TextEditingController controller;
  final FocusNode focusNode;
  final bool sending;
  final VoidCallback onSend;
  final List<SonderCommand> paletteMatches;
  final int paletteSelected;
  final bool paletteGrouped;
  final Map<String, String> paletteCategories;
  final ValueChanged<String> onPalettePick;
  final KeyEventResult Function(KeyEvent) onKey;
  final VoidCallback onOpenCommands;
  final bool desktop;

  /// Null when the mode is unknown, in which case nothing is shown.
  final PermissionMode? permissionMode;
  final bool permissionModeBusy;
  final VoidCallback onTapPermissionMode;

  const _InputBar({
    required this.controller,
    required this.focusNode,
    required this.sending,
    required this.onSend,
    required this.paletteMatches,
    required this.paletteSelected,
    required this.paletteGrouped,
    required this.paletteCategories,
    required this.onPalettePick,
    required this.onKey,
    required this.permissionMode,
    required this.permissionModeBusy,
    required this.onTapPermissionMode,
    required this.onOpenCommands,
    this.desktop = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 760),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (paletteMatches.isNotEmpty)
                  _CommandPalette(
                    matches: paletteMatches,
                    selected: paletteSelected,
                    grouped: paletteGrouped,
                    categories: paletteCategories,
                    onPick: onPalettePick,
                  ),
                Container(
                  decoration: BoxDecoration(
                    color: tokens.panel,
                    borderRadius: BorderRadius.circular(SonderRadius.sheet),
                    border: Border.all(color: tokens.hairlineStrong),
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Focus(
                        onKeyEvent: (node, event) => onKey(event),
                        child: TextField(
                          controller: controller,
                          focusNode: focusNode,
                          minLines: 1,
                          maxLines: 6,
                          textInputAction: TextInputAction.send,
                          onSubmitted: (_) => onSend(),
                          style: Theme.of(context).textTheme.bodyMedium,
                          decoration: const InputDecoration(
                            hintText: 'Message Sonder Runtime…',
                            filled: false,
                            contentPadding: EdgeInsets.fromLTRB(14, 12, 14, 6),
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                          ),
                        ),
                      ),
                      Padding(
                        padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                        child: Row(
                          children: [
                            // Read on the way to the send button rather than
                            // hidden behind a menu: what the agent will do
                            // without asking.
                            if (permissionMode != null) ...[
                              _PermissionModeChip(
                                state: permissionMode!,
                                busy: permissionModeBusy,
                                onTap: onTapPermissionMode,
                              ),
                              const SizedBox(width: 6),
                            ],
                            Tooltip(
                              message: 'Commands (Ctrl+K)',
                              child: InkWell(
                                onTap: onOpenCommands,
                                borderRadius:
                                    BorderRadius.circular(SonderRadius.pill),
                                child: Container(
                                  height: 28,
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 10,
                                  ),
                                  decoration: BoxDecoration(
                                    borderRadius: BorderRadius.circular(
                                      SonderRadius.pill,
                                    ),
                                    border: Border.all(
                                      color: tokens.hairlineStrong,
                                    ),
                                  ),
                                  child: Row(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                      Text(
                                        '/',
                                        style: tokens.mono(
                                          12,
                                          color: tokens.text2,
                                        ),
                                      ),
                                      if (desktop) ...[
                                        const SizedBox(width: 6),
                                        Text(
                                          'commands',
                                          style: tokens.mono(
                                            11,
                                            color: tokens.muted,
                                          ),
                                        ),
                                      ],
                                    ],
                                  ),
                                ),
                              ),
                            ),
                            Expanded(
                              child: desktop
                                  ? Padding(
                                      padding: const EdgeInsets.only(
                                        left: 8,
                                        right: 10,
                                      ),
                                      child: Text(
                                        'Enter send · Shift Enter newline',
                                        textAlign: TextAlign.right,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: tokens.mono(
                                          11,
                                          color: tokens.muted,
                                        ),
                                      ),
                                    )
                                  : const SizedBox.shrink(),
                            ),
                            SizedBox(
                              width: 32,
                              height: 32,
                              child: FloatingActionButton.small(
                                heroTag: null,
                                onPressed: sending ? null : onSend,
                                tooltip: 'Send',
                                child: sending
                                    ? const SizedBox(
                                        width: 16,
                                        height: 16,
                                        child: CircularProgressIndicator(
                                          strokeWidth: 2,
                                        ),
                                      )
                                    : const Icon(Icons.arrow_upward, size: 18),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// The always-visible autonomy indicator in the composer: what the agent
/// will do without asking, and — separately — whether it is elevated.
class _PermissionModeChip extends StatelessWidget {
  final PermissionMode state;
  final bool busy;
  final VoidCallback onTap;

  const _PermissionModeChip({
    required this.state,
    required this.busy,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final tone = permissionModeColor(Theme.of(context).colorScheme, state.mode);
    final modeLabel = state.displayLabel;
    final modeDescription = state.blurb.trim().isEmpty
        ? 'Autonomy mode'
        : '${state.displayLabel}: ${state.blurb}';
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Tooltip(
          message: state.blurb.trim().isEmpty
              ? 'Autonomy mode — tap to change'
              : '${state.displayLabel} — ${state.blurb}',
          child: Semantics(
            button: true,
            label: modeDescription,
            hint: busy ? 'Changing mode' : 'Double tap to change mode',
            child: InkWell(
              key: const Key('permission-mode-chip'),
              onTap: busy ? null : onTap,
              borderRadius: BorderRadius.circular(SonderRadius.pill),
              child: Container(
                height: 28,
                padding: const EdgeInsets.fromLTRB(10, 0, 6, 0),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(SonderRadius.pill),
                  border: Border.all(color: tokens.hairlineStrong),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      width: 7,
                      height: 7,
                      decoration: BoxDecoration(
                        color: tone,
                        borderRadius: BorderRadius.circular(4),
                      ),
                    ),
                    const SizedBox(width: 6),
                    Icon(permissionModeIcon(state.mode), size: 13, color: tone),
                    const SizedBox(width: 5),
                    Text(
                      modeLabel,
                      style: tokens.mono(12, weight: FontWeight.w500),
                    ),
                    if (busy)
                      Padding(
                        padding: const EdgeInsets.only(left: 6, right: 2),
                        child: SizedBox(
                          width: 11,
                          height: 11,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: tokens.text2,
                          ),
                        ),
                      )
                    else
                      Icon(Icons.expand_more, size: 16, color: tokens.muted),
                  ],
                ),
              ),
            ),
          ),
        ),
        if (state.elevated) ...[
          const SizedBox(width: 6),
          _ElevatedBadge(reason: state.elevationReason),
        ],
      ],
    );
  }
}

/// The privilege axis, as its own badge outside the mode chip.
///
/// Elevation is a different question from autonomy — `permission_modes.py`
/// grants it from no mode at all — so it is kept distinct on every channel
/// available: it sits outside the chip, is outlined rather than filled, uses
/// the theme's danger tone rather than a mode colour, carries a shield, and
/// is set in spaced capitals. Folding it into the label as "auto +admin"
/// would read as a fifth mode, which is exactly the confusion to avoid.
class _ElevatedBadge extends StatelessWidget {
  final String reason;
  const _ElevatedBadge({required this.reason});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Tooltip(
      message: reason.trim().isEmpty
          ? 'Elevated privileges are on. This is separate from the mode — no '
              'mode turns it on.'
          : 'Elevated privileges are on: ${reason.trim()}',
      child: Semantics(
        label: 'Elevated privileges: on',
        hint: reason.trim().isEmpty ? null : reason.trim(),
        child: Container(
          key: const Key('permission-elevated-badge'),
          height: 28,
          padding: const EdgeInsets.symmetric(horizontal: 8),
          decoration: BoxDecoration(
            color: tokens.dangerDim,
            borderRadius: BorderRadius.circular(SonderRadius.control),
            border: Border.all(color: tokens.danger, width: 1.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.shield_outlined, size: 13, color: tokens.danger),
              const SizedBox(width: 4),
              Text(
                'ADMIN',
                style: tokens.mono(
                  11,
                  color: tokens.danger,
                  weight: FontWeight.w600,
                ).copyWith(letterSpacing: 1.0),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The mode picker: every mode the server publishes, with the blurb that says
/// where its boundary is, and the privilege axis called out as separate.
class _PermissionModeDialog extends StatelessWidget {
  final PermissionMode state;
  const _PermissionModeDialog({required this.state});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return AlertDialog(
      key: const Key('permission-mode-picker'),
      title: const Text('Autonomy mode'),
      contentPadding: const EdgeInsets.fromLTRB(0, 12, 0, 0),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final option in state.options)
                InkWell(
                  key: Key('permission-mode-option-${option.name}'),
                  onTap: () => Navigator.of(context).pop(option.name),
                  child: Container(
                    width: double.infinity,
                    color: option.name == state.mode
                        ? cs.primary.withValues(alpha: 0.10)
                        : null,
                    padding: const EdgeInsets.symmetric(
                      horizontal: 20,
                      vertical: 10,
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Padding(
                          padding: const EdgeInsets.only(top: 2),
                          child: Icon(
                            permissionModeIcon(option.name),
                            size: 16,
                            color: permissionModeColor(cs, option.name),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                option.displayLabel,
                                style: TextStyle(
                                  fontWeight: option.name == state.mode
                                      ? FontWeight.w700
                                      : FontWeight.w500,
                                  color: permissionModeColor(cs, option.name),
                                ),
                              ),
                              if (option.blurb.trim().isNotEmpty)
                                Text(
                                  option.blurb,
                                  style: TextStyle(
                                    fontSize: 12,
                                    color: cs.onSurfaceVariant,
                                  ),
                                ),
                            ],
                          ),
                        ),
                        if (option.name == state.mode)
                          Icon(Icons.check, size: 18, color: cs.primary),
                      ],
                    ),
                  ),
                ),
              const Divider(height: 20),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: Text(
                  state.elevated
                      ? 'Privilege: elevated — a separate switch. No mode '
                          'grants it, and changing mode does not turn it off.'
                      : 'Privilege: normal. Elevation is a separate switch — '
                          'no mode grants it.',
                  style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
                ),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
      ],
    );
  }
}
