import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'chat_store.dart';
import 'models.dart';
import 'settings.dart';
import 'settings_screen.dart';
import 'system_screen.dart';

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

class _ChatScreenState extends State<ChatScreen> {
  final _messages = <ChatMessage>[];
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _inputFocus = FocusNode();
  List<ChatThread> _threads = const [];
  String _currentThreadId = '';
  String _project = 'default';
  bool _sending = false;
  bool _loadingThreads = true;
  Timer? _statusTimer;
  SystemInfo? _systemInfo;

  // The model/tier to answer with. "trilobite" is the local self-improving student;
  // other entries route to that model on the server. Populated from
  // GET /v1/models, with a sensible fallback if the server is unreachable.
  late String _model;
  List<String> _models = const ['trilobite'];

  // Quick-access slash commands the serve layer understands.
  static const _quickCommands = <String, String>{
    '/stats': 'Show learning stats',
    '/context': 'Show context health',
    '/compact': 'Preview context compaction',
    '/commands': 'List command registry',
    '/dump': 'Save chat/debug dump',
    '/todo': 'Show visible task state',
    '/quality': 'Audit memory quality',
    '/improve': 'Show next improvements',
    '/agents': 'Show live agent activity',
    '/permissions': 'Show permission rules',
    '/master': 'Choose inline or delegated execution',
    '/runwindow': 'Launch last code in a Windows console',
    '/help': 'List commands',
    '/train': 'Practice & self-learn',
    '/pass': 'Mark last answer good',
    '/accept': 'Mark last answer useful',
    '/edited': 'Mark answer used after edits',
    '/fail': 'Mark last answer bad',
  };

  TrilobiteApi get _api => TrilobiteApi(
        baseUrl: widget.settings.serverUrl,
        apiKey: widget.settings.apiKey,
      );

  @override
  void initState() {
    super.initState();
    _model = widget.settings.model;
    _loadThreads();
    _refreshModels();
    _refreshStatus();
    _statusTimer = Timer.periodic(
      const Duration(seconds: 5),
      (_) => _refreshStatus(),
    );
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
        if (!_models.contains(_model)) _model = _models.first;
      });
    } catch (_) {
      // Offline / no auth — keep the static fallback list.
    }
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
      await _api.chat([
        ChatMessage(role: Role.user, content: command),
      ],
          model: _model,
          contextSize: widget.settings.contextSize,
          sessionId: _currentThreadId,
          project: _project);
    } catch (_) {
      // Passive learning should never interrupt the chat UI.
    }
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
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

  Future<void> _send([String? preset]) async {
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty || _sending) return;

    setState(() {
      _messages.add(ChatMessage(role: Role.user, content: text));
      _messages.add(const ChatMessage(
          role: Role.assistant, content: '', pending: true));
      _sending = true;
      if (preset == null) _input.clear();
    });
    _scrollToEnd();

    try {
      // Send everything except the trailing pending placeholder.
      final history = _messages.sublist(0, _messages.length - 1);
      final reply = await _api.chat(
        history,
        model: _model,
        contextSize: widget.settings.contextSize,
        sessionId: _currentThreadId,
        project: _project,
      );
      setState(() {
        _messages[_messages.length - 1] = ChatMessage(
          role: Role.assistant,
          content: reply.isEmpty ? '(empty response)' : reply,
        );
      });
    } on TrilobiteException catch (e) {
      setState(() {
        _messages[_messages.length - 1] = ChatMessage(
          role: Role.assistant,
          content: e.message,
          error: true,
        );
      });
    } finally {
      if (mounted) {
        await _saveCurrentThread();
        setState(() => _sending = false);
        _refreshStatus();
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
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => SettingsScreen(
        settings: widget.settings,
        onChanged: widget.onSettingsChanged,
      ),
    ));
    setState(() {
      // Pick up server/key/model changes; re-fetch the model list if it moved.
      _model = widget.settings.model;
    });
    _refreshModels();
  }

  Future<void> _openSystem() async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => SystemScreen(settings: widget.settings),
    ));
  }

  String _modelLabel(String m) => m == 'trilobite' ? 'trilobite (local)' : m;

  @override
  Widget build(BuildContext context) {
    final currentTitle =
        _loadingThreads ? 'Loading chats...' : _currentThread.displayTitle;
    return Scaffold(
      drawer: _ChatDrawer(
        threads: _threads,
        currentThreadId: _currentThreadId,
        onNew: _newChat,
        onSelect: _switchThread,
        onDelete: _deleteThread,
      ),
      appBar: AppBar(
        title: Row(
          children: [
            const Text('🦑 ', style: TextStyle(fontSize: 20)),
            // Model picker: switch which LLM answers, per conversation.
            PopupMenuButton<String>(
              tooltip: 'Choose model',
              onSelected: _selectModel,
              itemBuilder: (_) => _models
                  .map((m) => PopupMenuItem<String>(
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
                      ))
                  .toList(),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Flexible(
                    child: Text(
                      _modelLabel(_model),
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontSize: 18, fontWeight: FontWeight.w500),
                    ),
                  ),
                  const Icon(Icons.arrow_drop_down),
                ],
              ),
            ),
          ],
        ),
        actions: [
          PopupMenuButton<String>(
            tooltip: 'Commands',
            icon: const Icon(Icons.bolt_outlined),
            onSelected: (c) => _send(c),
            itemBuilder: (_) => _quickCommands.entries
                .map((e) => PopupMenuItem<String>(
                      value: e.key,
                      child: ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(e.key),
                        subtitle: Text(e.value),
                      ),
                    ))
                .toList(),
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
      body: Column(
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
                    padding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 16),
                    itemCount: _messages.length,
                    itemBuilder: (_, i) => _Bubble(
                      message: _messages[i],
                      onPassive: _recordPassive,
                    ),
                  ),
          ),
          _InputBar(
            controller: _input,
            focusNode: _inputFocus,
            sending: _sending,
            onSend: () => _send(),
          ),
          _LiveStatusBar(info: _systemInfo, model: _model, project: _project),
        ],
      ),
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
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('🦑', style: TextStyle(fontSize: 56)),
            const SizedBox(height: 16),
            Text('Your private AI',
                style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 8),
            Text(
              'Connected to $serverUrl',
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
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
                    'Write a Python function to parse a CSV', onQuick),
                _Suggestion('Explain async/await simply', onQuick),
                _Suggestion('/stats', onQuick),
              ],
            ),
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
    final cs = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: cs.surface,
        border: Border(bottom: BorderSide(color: cs.outlineVariant)),
      ),
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
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
                  style: Theme.of(context).textTheme.titleSmall,
                ),
                const SizedBox(height: 2),
                Text(
                  '$messageCount messages',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: cs.outline,
                      ),
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

  const _ChatDrawer({
    required this.threads,
    required this.currentThreadId,
    required this.onNew,
    required this.onSelect,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final projects = threads.map((t) => t.project).toSet().toList()..sort();
    return Drawer(
      child: SafeArea(
        child: Column(
          children: [
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
    return ActionChip(
      label: Text(text),
      onPressed: () => onTap(text),
    );
  }
}

class _LiveStatusBar extends StatelessWidget {
  final SystemInfo? info;
  final String model;
  final String project;

  const _LiveStatusBar({
    required this.info,
    required this.model,
    required this.project,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final contextInfo = info?.context;
    final agentInfo = info?.agents;
    final activeProject = project.trim().isEmpty
        ? (contextInfo?.project ?? 'unknown')
        : project.trim();
    final projectText = activeProject == 'none'
        ? 'project: none'
        : 'project: $activeProject';
    final path = info?.stateHome ?? '';
    var latest = 'idle';
    if (agentInfo != null) {
      if (agentInfo.agents.isNotEmpty) {
        final first = agentInfo.agents.first;
        latest = '${first.id}: ${first.activity}';
      } else if (agentInfo.events.isNotEmpty) {
        latest = agentInfo.events.last;
      }
    }
    final parts = [
      'ctx ${(contextInfo?.contextPercent ?? 0).toStringAsFixed(1)}%',
      'native ${contextInfo?.nativeContextLimit ?? 0}',
      contextInfo?.contextMode ?? 'native',
      'agents ${agentInfo?.activeAgents ?? 0}',
      projectText,
      'tokens ${agentInfo?.tokensIn ?? 0}/${agentInfo?.tokensOut ?? 0}',
      'model $model',
      if (path.isNotEmpty) path,
      latest,
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
        child: Text(
          parts.join('   |   '),
          maxLines: 1,
          style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: cs.onSurfaceVariant,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
        ),
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

    final Color bg;
    final Color fg;
    if (isUser) {
      bg = cs.primary;
      fg = cs.onPrimary;
    } else if (message.error) {
      bg = cs.errorContainer;
      fg = cs.onErrorContainer;
    } else {
      bg = cs.surfaceContainerHighest;
      fg = cs.onSurface;
    }

    Widget content;
    if (message.pending) {
      content = const SizedBox(
        height: 18,
        width: 40,
        child: _TypingDots(),
      );
    } else {
      content = SelectableText(
        message.content,
        style: TextStyle(color: fg, height: 1.35),
      );
    }

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.82,
        ),
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: bg,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16),
            topRight: const Radius.circular(16),
            bottomLeft: Radius.circular(isUser ? 16 : 4),
            bottomRight: Radius.circular(isUser ? 4 : 16),
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            content,
            if (!isUser && !message.pending && message.content.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Wrap(
                  spacing: 10,
                  children: [
                    InkWell(
                      onTap: () {
                        Clipboard.setData(ClipboardData(text: message.content));
                        onPassive?.call('/copied');
                      },
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.copy_all_outlined,
                              size: 14, color: fg.withValues(alpha: 0.6)),
                          const SizedBox(width: 4),
                          Text('copy',
                              style: TextStyle(
                                  fontSize: 11,
                                  color: fg.withValues(alpha: 0.6))),
                        ],
                      ),
                    ),
                    InkWell(
                      onTap: () => onPassive?.call('/accept'),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.check_circle_outline,
                              size: 14, color: fg.withValues(alpha: 0.6)),
                          const SizedBox(width: 4),
                          Text('useful',
                              style: TextStyle(
                                  fontSize: 11,
                                  color: fg.withValues(alpha: 0.6))),
                        ],
                      ),
                    ),
                    InkWell(
                      onTap: () => onPassive?.call('/edited'),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.edit_outlined,
                              size: 14, color: fg.withValues(alpha: 0.6)),
                          const SizedBox(width: 4),
                          Text('edited',
                              style: TextStyle(
                                  fontSize: 11,
                                  color: fg.withValues(alpha: 0.6))),
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

class _TypingDots extends StatefulWidget {
  const _TypingDots();
  @override
  State<_TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<_TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 900))
        ..repeat();

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

class _InputBar extends StatelessWidget {
  final TextEditingController controller;
  final FocusNode focusNode;
  final bool sending;
  final VoidCallback onSend;

  const _InputBar({
    required this.controller,
    required this.focusNode,
    required this.sending,
    required this.onSend,
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
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TextField(
                controller: controller,
                focusNode: focusNode,
                minLines: 1,
                maxLines: 6,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => onSend(),
                decoration: InputDecoration(
                  hintText: 'Message trilobite…',
                  filled: true,
                  fillColor: cs.surfaceContainerHighest,
                  contentPadding: const EdgeInsets.symmetric(
                      horizontal: 16, vertical: 12),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(24),
                    borderSide: BorderSide.none,
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
      ),
    );
  }
}
