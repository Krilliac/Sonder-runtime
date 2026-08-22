import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

import 'api.dart';
import 'chat_store.dart';
import 'models.dart';
import 'safety_colors.dart';
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
        matches = _catalog.commands
            .where((c) => c.matchesPrefix(query))
            .toList();
        // Nothing starts with it -- fall back to matching the summary and
        // category too, so "/memory" still finds the quality and privacy
        // audits.
        if (matches.isEmpty) {
          final needle = query.substring(1);
          matches = _catalog.commands
              .where((c) => c.matchesLoose(needle))
              .toList();
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
      if (event.isShiftPressed) {
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
    final nextMessages = (messages ?? _messages)
        .where((m) => !m.pending)
        .toList();
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
    }).toList()..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    setState(() {
      _threads = updated;
      _project = nextProject;
    });
    await ChatStore.save(updated);
  }

  String _titleForMessages(List<ChatMessage> messages) {
    final userMessages = messages.where((m) => m.role == Role.user);
    if (userMessages.isEmpty) return _currentThread.title;
    final text = userMessages.first.content
        .replaceAll(RegExp(r'\s+'), ' ')
        .trim();
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

    final isVerboseTopic =
        t.startsWith('/verbose') ||
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
        );
      });
    } on SonderException catch (e) {
      setState(() {
        _messages[_messages.length - 1] = ChatMessage(
          role: Role.assistant,
          // Verbose mode appends the original exception. It lives client-side
          // on purpose: the errors worth reading raw are transport failures,
          // and during one of those a server-side toggle is exactly the thing
          // you cannot reach.
          content: _verboseErrors && e.cause != null
              ? '${e.message}\n\n---\n```\n${e.cause}\n```'
              : e.message,
          error: true,
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
    final next = remaining.isEmpty
        ? [ChatThread.fresh(project: _project)]
        : remaining;
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
      MaterialPageRoute(
        builder: (_) => SettingsScreen(
          settings: widget.settings,
          onChanged: widget.onSettingsChanged,
        ),
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
    final currentTitle = _loadingThreads
        ? 'Loading chats...'
        : _currentThread.displayTitle;
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
        );
        return Shortcuts(
          shortcuts: <ShortcutActivator, Intent>{
            const SingleActivator(LogicalKeyboardKey.keyK, control: true):
                const _OpenCommandBrowserIntent(),
            const SingleActivator(LogicalKeyboardKey.keyN, control: true):
                const _NewChatIntent(),
            const SingleActivator(LogicalKeyboardKey.keyP, control: true):
                const _OpenThreadSwitcherIntent(),
            const SingleActivator(LogicalKeyboardKey.comma, control: true):
                const _OpenSettingsIntent(),
            const SingleActivator(LogicalKeyboardKey.keyD, control: true):
                const _OpenSystemIntent(),
            const SingleActivator(
              LogicalKeyboardKey.tab,
              shift: true,
            ): const _CyclePermissionModeIntent(),
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
            title: Row(
              children: [
                Container(
                  width: 32,
                  height: 32,
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.primaryContainer,
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Icon(
                    Icons.hub_outlined,
                    size: 19,
                    color: Theme.of(context).colorScheme.onPrimaryContainer,
                  ),
                ),
                const SizedBox(width: 10),
                // Model picker: switch which LLM answers, per conversation.
                PopupMenuButton<String>(
                  tooltip: 'Choose inference route or model',
                  onSelected: _selectModel,
                  itemBuilder: (_) => _models
                      .map(
                        (m) => PopupMenuItem<String>(
                          value: m,
                          child: Row(
                            children: [
                              if (m == _model)
                                const Icon(Icons.check, size: 18)
                              else
                                const SizedBox(width: 18),
                              const SizedBox(width: 8),
                              Text(_modelLabel(m)),
                            ],
                          ),
                        ),
                      )
                      .toList(),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Flexible(
                        child: Text(
                          _modelLabel(_model),
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontSize: 17,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      const Icon(Icons.arrow_drop_down),
                    ],
                  ),
                ),
              ],
            ),
            actions: [
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
          ),
          body: Row(
            children: [
              if (desktop) SizedBox(width: 304, child: drawer),
              Expanded(
                child: Column(
                  children: [
                    _ChatHeader(
                      title: currentTitle,
                      project: _project,
                      messageCount: _messages.where((m) => !m.pending).length,
                      onEditProject: _editProject,
                    ),
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
                                18,
                                16,
                                20,
                              ),
                              itemCount: _messages.length,
                              itemBuilder: (_, i) => Center(
                                child: ConstrainedBox(
                                  constraints: const BoxConstraints(
                                    maxWidth: 1080,
                                  ),
                                  child: _Bubble(
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
                    ),
                    _LiveStatusBar(
                      info: _systemInfo,
                      model: _model,
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
    final cs = Theme.of(context).colorScheme;
    return LayoutBuilder(
      builder: (context, constraints) {
        final minHeight = constraints.maxHeight > 64
            ? constraints.maxHeight - 64
            : 0.0;
        return SingleChildScrollView(
          padding: const EdgeInsets.all(32),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: minHeight),
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Container(
                    width: 88,
                    height: 88,
                    decoration: BoxDecoration(
                      color: cs.primaryContainer,
                      borderRadius: BorderRadius.circular(28),
                      boxShadow: [
                        BoxShadow(
                          color: cs.primary.withValues(alpha: 0.18),
                          blurRadius: 28,
                          spreadRadius: 2,
                        ),
                      ],
                    ),
                    child: Icon(
                      Icons.hub_outlined,
                      size: 46,
                      color: cs.onPrimaryContainer,
                    ),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'Sonder Runtime',
                    style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                      fontWeight: FontWeight.w800,
                      letterSpacing: -0.5,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Not a standalone model: Sonder Runtime supplies routing, prompts, '
                    'memory, tools, and policy to model weights served locally by '
                    'Ollama.',
                    style: Theme.of(context).textTheme.bodyMedium,
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Connected to $serverUrl',
                    style: Theme.of(context).textTheme.bodySmall
                        ?.copyWith(color: cs.outline),
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 24),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    alignment: WrapAlignment.center,
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
        );
      },
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
    final cs = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: cs.surface.withValues(alpha: 0.72),
        border: Border(bottom: BorderSide(color: cs.outlineVariant)),
      ),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleSmall
                      ?.copyWith(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 2),
                Text(
                  '$messageCount messages',
                  style: Theme.of(context).textTheme.bodySmall
                      ?.copyWith(color: cs.outline),
                ),
              ],
            ),
          ),
          ActionChip(
            avatar: const Icon(Icons.folder_outlined, size: 16),
            label: Text(project.trim().isEmpty ? 'default' : project),
            onPressed: onEditProject,
          ),
        ],
      ),
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

  const _ChatDrawer({
    required this.threads,
    required this.currentThreadId,
    required this.onNew,
    required this.onSelect,
    required this.onDelete,
    this.embedded = false,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final projects = threads.map((t) => t.project).toSet().toList()..sort();
    return Drawer(
      child: SafeArea(
        child: Column(
          children: [
            if (embedded)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
                child: Row(
                  children: [
                    Container(
                      width: 32,
                      height: 32,
                      decoration: BoxDecoration(
                        color: cs.primaryContainer,
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Icon(
                        Icons.hub_outlined,
                        size: 18,
                        color: cs.onPrimaryContainer,
                      ),
                    ),
                    const SizedBox(width: 10),
                    const Expanded(
                      child: Text(
                        'Sonder Runtime\nLocal-first workspace',
                        style: TextStyle(
                          fontWeight: FontWeight.w700,
                          height: 1.25,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 12, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      'Chats',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  IconButton(
                    tooltip: 'New chat',
                    onPressed: () {
                      unawaited(Navigator.of(context).maybePop());
                      onNew();
                    },
                    icon: const Icon(Icons.add_comment_outlined),
                  ),
                ],
              ),
            ),
            if (projects.isNotEmpty)
              SizedBox(
                height: 42,
                child: ListView.separated(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  scrollDirection: Axis.horizontal,
                  itemCount: projects.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 6),
                  itemBuilder: (_, index) => Chip(
                    visualDensity: VisualDensity.compact,
                    label: Text(projects[index]),
                    avatar: const Icon(Icons.folder_outlined, size: 16),
                  ),
                ),
              ),
            const Divider(height: 1),
            Expanded(
              child: threads.isEmpty
                  ? const Center(child: Text('No chats yet'))
                  : ListView.builder(
                      itemCount: threads.length,
                      itemBuilder: (_, index) {
                        final thread = threads[index];
                        final selected = thread.id == currentThreadId;
                        return ListTile(
                          selected: selected,
                          leading: CircleAvatar(
                            backgroundColor: selected
                                ? cs.primaryContainer
                                : cs.surfaceContainerHighest,
                            child: Icon(
                              Icons.chat_bubble_outline,
                              color: selected
                                  ? cs.onPrimaryContainer
                                  : cs.onSurfaceVariant,
                              size: 18,
                            ),
                          ),
                          title: Text(
                            thread.displayTitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: Text(
                            '${thread.project}  |  ${thread.messages.length} messages',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          trailing: IconButton(
                            tooltip: 'Delete chat',
                            onPressed: threads.length <= 1
                                ? null
                                : () => onDelete(thread),
                            icon: const Icon(Icons.delete_outline),
                          ),
                          onTap: () => onSelect(thread),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Suggestion extends StatelessWidget {
  final String text;
  final ValueChanged<String> onTap;
  const _Suggestion(this.text, this.onTap);

  @override
  Widget build(BuildContext context) {
    return ActionChip(label: Text(text), onPressed: () => onTap(text));
  }
}

class _LiveStatusBar extends StatelessWidget {
  final SystemInfo? info;
  final String model;

  const _LiveStatusBar({
    required this.info,
    required this.model,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
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
        : '+${responseInfo.linesAdded} −${responseInfo.linesDeleted} · '
            '$turnTokens tok';
    final segments = <_StatusMetric>[
      _StatusMetric('Context', contextText),
      _StatusMetric('Activity', info?.executionSummary ?? '—'),
      _StatusMetric('Route', routeText),
      if (turnText != null) _StatusMetric('Turn', turnText),
    ];
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest.withValues(alpha: 0.65),
        border: Border(top: BorderSide(color: cs.outlineVariant)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            for (var i = 0; i < segments.length; i++) ...[
              if (i > 0)
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  child: Text('·', style: TextStyle(color: cs.outline)),
                ),
              _StatusMetricView(metric: segments[i]),
            ],
          ],
        ),
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
    final textTheme = Theme.of(context).textTheme;
    final cs = Theme.of(context).colorScheme;
    return Semantics(
      key: ValueKey('status-metric-${metric.label.toLowerCase()}'),
      label: '${metric.label}: ${metric.value}',
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            metric.label.toLowerCase(),
            style: textTheme.labelSmall?.copyWith(
              color: cs.onSurfaceVariant,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.5,
            ),
          ),
          const SizedBox(width: 5),
          Text(
            metric.value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: textTheme.bodySmall?.copyWith(
              color: cs.onSurface,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  final ChatMessage message;
  final ValueChanged<String>? onPassive;
  const _Bubble({required this.message, this.onPassive});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final isUser = message.role == Role.user;
    final viewport = MediaQuery.sizeOf(context).width;

    final Color bg;
    final Color fg;
    if (isUser) {
      bg = cs.primary;
      fg = cs.onPrimary;
    } else if (message.error) {
      bg = cs.errorContainer;
      fg = cs.onErrorContainer;
    } else {
      bg = cs.surfaceContainerLow;
      fg = cs.onSurface;
    }

    Widget content;
    if (message.pending) {
      content = const SizedBox(height: 18, width: 40, child: _TypingDots());
    } else {
      content = isUser
          ? SelectableText(
              message.content,
              style: TextStyle(color: fg, height: 1.4),
            )
          : _AssistantContent(
              content: message.content,
              color: fg,
              reasoning: message.reasoning,
            );
    }

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: viewport < 760 ? viewport - 32 : (isUser ? 720 : 960),
        ),
        margin: const EdgeInsets.symmetric(vertical: 6),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        decoration: BoxDecoration(
          color: bg,
          border: isUser
              ? null
              : Border.all(color: cs.outlineVariant.withValues(alpha: 0.65)),
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(18),
            topRight: const Radius.circular(18),
            bottomLeft: Radius.circular(isUser ? 18 : 6),
            bottomRight: Radius.circular(isUser ? 6 : 18),
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (!isUser && !message.pending) ...[
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Container(
                    width: 24,
                    height: 24,
                    decoration: BoxDecoration(
                      color: cs.primaryContainer,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Icon(
                      Icons.hub_outlined,
                      size: 15,
                      color: cs.onPrimaryContainer,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    'Sonder Runtime',
                    style: Theme.of(context).textTheme.labelLarge?.copyWith(
                      color: cs.onSurfaceVariant,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.1,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),
            ],
            content,
            if (!isUser && !message.pending && message.content.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Wrap(
                  spacing: 10,
                  children: [
                    _FeedbackAction(
                      icon: Icons.copy_all_outlined,
                      label: 'Copy response',
                      text: 'copy',
                      color: fg,
                      onTap: () {
                        Clipboard.setData(ClipboardData(text: message.content));
                        onPassive?.call('/copied');
                      },
                    ),
                    // Quality feedback trains the learning loop on the last
                    // answer. An error bubble is a transport or server
                    // failure, not an answer, so rating it would teach the
                    // loop about a turn the model never produced. Copy stays:
                    // copying the failure text is exactly what you want to do
                    // with it.
                    if (!message.error) ...[
                      _FeedbackAction(
                        icon: Icons.check_circle_outline,
                        label: 'Mark response useful',
                        text: 'useful',
                        color: fg,
                        onTap: () => onPassive?.call('/accept'),
                      ),
                      _FeedbackAction(
                        icon: Icons.edit_outlined,
                        label: 'Mark response edited',
                        text: 'edited',
                        color: fg,
                        onTap: () => onPassive?.call('/edited'),
                      ),
                    ],
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
  final Color color;
  final VoidCallback onTap;

  const _FeedbackAction({
    required this.icon,
    required this.label,
    required this.text,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final muted = color.withValues(alpha: 0.6);
    return Semantics(
      button: true,
      label: label,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(8),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48, minWidth: 48),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(icon, size: 14, color: muted),
                const SizedBox(width: 4),
                Text(text, style: TextStyle(fontSize: 11, color: muted)),
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

  const _AssistantContent({
    required this.content,
    required this.color,
    this.reasoning = '',
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final markerIndex = content.indexOf(_activityMarker);
    final answer =
        (markerIndex < 0 ? content : content.substring(0, markerIndex))
            .trimRight();
    final activity = markerIndex < 0
        ? ''
        : content.substring(markerIndex).trim();
    final body = Theme.of(context).textTheme.bodyMedium
        ?.copyWith(color: color, height: 1.48);
    final markdownStyle = MarkdownStyleSheet.fromTheme(Theme.of(context))
        .copyWith(
          p: body,
          strong: body?.copyWith(fontWeight: FontWeight.w700),
          a: body?.copyWith(
            color: cs.primary,
            decoration: TextDecoration.underline,
            decorationColor: cs.primary.withValues(alpha: 0.6),
          ),
          code: body?.copyWith(
            fontFamily: 'Consolas',
            fontSize: 13,
            color: cs.onSurface,
            backgroundColor: cs.surfaceContainerHighest,
          ),
          codeblockPadding: const EdgeInsets.all(14),
          codeblockDecoration: BoxDecoration(
            color: cs.surfaceContainerHighest.withValues(alpha: 0.88),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: cs.outlineVariant),
          ),
          blockquoteDecoration: BoxDecoration(
            color: cs.primaryContainer.withValues(alpha: 0.22),
            borderRadius: BorderRadius.circular(10),
            border: Border(left: BorderSide(color: cs.primary, width: 3)),
          ),
          blockquotePadding: const EdgeInsets.fromLTRB(14, 10, 12, 10),
          blockSpacing: 10,
          listIndent: 24,
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
      ],
    );
  }
}

/// A collapsed, monospaced detail block under an answer.
///
/// Shared by the reasoning and activity sections so they stay visually
/// identical as either one changes.
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
    final cs = Theme.of(context).colorScheme;
    return Material(
      color: Colors.transparent,
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          dense: true,
          visualDensity: VisualDensity.compact,
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          leading: Icon(icon, size: 17, color: cs.primary),
          title: Text(
            label,
            style: Theme.of(context).textTheme.labelMedium?.copyWith(
              color: cs.onSurfaceVariant,
              fontWeight: FontWeight.w700,
            ),
          ),
          children: [
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: cs.surfaceContainerHighest.withValues(alpha: 0.7),
                borderRadius: BorderRadius.circular(10),
              ),
              child: SelectableText(
                body,
                style: TextStyle(
                  color: cs.onSurfaceVariant,
                  fontFamily: 'Consolas',
                  fontSize: 11,
                  height: 1.4,
                ),
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
    return InkWell(
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
                      fontFamily: 'monospace',
                      fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
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
            if (usage != command.displayName)
              Padding(
                padding: const EdgeInsets.only(left: 17, top: 2),
                child: Text(
                  usage,
                  style: TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 11,
                    color: cs.onSurfaceVariant.withValues(alpha: 0.75),
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ],
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

  const _PaletteRow.heading(this.heading) : command = null, matchIndex = -1;
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
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
        decoration: BoxDecoration(
          color: cs.surface,
          border: Border(top: BorderSide(color: cs.outlineVariant)),
        ),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1080),
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
                // Sits directly above the composer so it is read on the way to
                // the send button, rather than hidden behind a menu.
                if (permissionMode != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Align(
                      alignment: Alignment.centerLeft,
                      child: _PermissionModeChip(
                        state: permissionMode!,
                        busy: permissionModeBusy,
                        onTap: onTapPermissionMode,
                      ),
                    ),
                  ),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Expanded(
                      child: Focus(
                        onKeyEvent: (node, event) => onKey(event),
                        child: TextField(
                          controller: controller,
                          focusNode: focusNode,
                          minLines: 1,
                          maxLines: 6,
                          textInputAction: TextInputAction.send,
                          onSubmitted: (_) => onSend(),
                          decoration: InputDecoration(
                            hintText: 'Message Sonder Runtime…',
                            filled: true,
                            fillColor: cs.surfaceContainerHighest,
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 16,
                              vertical: 12,
                            ),
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(24),
                              borderSide: BorderSide.none,
                            ),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    FloatingActionButton.small(
                      onPressed: sending ? null : onSend,
                      elevation: 0,
                      child: sending
                          ? const SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Icon(Icons.arrow_upward),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// The always-visible autonomy indicator above the composer: what the agent
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
    final cs = Theme.of(context).colorScheme;
    final fill = permissionModeColor(cs, state.mode);
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
              borderRadius: BorderRadius.circular(20),
              child: Container(
                padding: const EdgeInsets.fromLTRB(10, 4, 6, 4),
                decoration: BoxDecoration(
                  color: fill,
                  borderRadius: BorderRadius.circular(20),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      permissionModeIcon(state.mode),
                      size: 14,
                      color: Colors.white,
                    ),
                    const SizedBox(width: 6),
                    Text(
                      modeLabel,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (busy)
                      const Padding(
                        padding: EdgeInsets.only(left: 6, right: 2),
                        child: SizedBox(
                          width: 11,
                          height: 11,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white,
                          ),
                        ),
                      )
                    else
                      const Icon(
                        Icons.arrow_drop_down,
                        size: 18,
                        color: Colors.white,
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
        if (state.elevated) ...[
          const SizedBox(width: 8),
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
/// the theme's error colour rather than a mode colour, carries a shield, and
/// is set in spaced capitals. Folding it into the label as "auto +admin"
/// would read as a fifth mode, which is exactly the confusion to avoid.
class _ElevatedBadge extends StatelessWidget {
  final String reason;
  const _ElevatedBadge({required this.reason});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
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
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: cs.errorContainer,
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: cs.error, width: 1.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                Icons.shield_outlined,
                size: 13,
                color: cs.onErrorContainer,
              ),
              const SizedBox(width: 4),
              Text(
                'ADMIN',
                style: TextStyle(
                  color: cs.onErrorContainer,
                  fontSize: 11,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 1.0,
                ),
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
