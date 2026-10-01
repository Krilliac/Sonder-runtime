import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../agent_screen.dart';
import '../chat/connection.dart';
import '../chat/transcript.dart' show announceToScreenReader;
import '../chat_screen.dart';
import '../models.dart';
import '../settings.dart';
import '../settings_screen.dart';
import '../system_screen.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../workspace_ui.dart';
import 'page_navigator.dart';
import 'preferences.dart';
import 'shortcuts.dart';
import 'sidebar.dart';
import 'signals.dart';
import 'thread_switcher.dart';

/// Window width at which the sidebar sits beside the page. Below it the
/// page fills the window and the sidebar is a drawer.
const double kShellWideBreakpoint = 1000;

/// Builds a destination page in place of the real one (tests). Return null
/// to keep the default page.
typedef ShellPageBuilder = Widget? Function(BuildContext context,
    WorkspaceDestination destination, ShellSection? section);

/// The app's one persistent frame, in the manner of the Claude and Codex
/// desktop apps: a sidebar (wide) or drawer (narrow) with New chat, Search,
/// the four peer destinations and the conversation list, around one
/// destination page at a time.
///
/// * Chat stays mounted while another destination is shown (offstage, its
///   tickers paused), so a streaming turn keeps running and the composer
///   keeps its draft. The other destinations are built when opened.
/// * Pages find a [ShellScope]: they drop their own way back to Chat, show
///   a menu button on narrow layouts, can register a [ShellLeaveGuard], and
///   can be opened at a section.
/// * The shell owns the app-wide shortcuts (see [shellShortcuts]).
class AppShell extends StatefulWidget {
  final Settings settings;
  final ValueChanged<Settings> onSettingsChanged;
  final ChatBackendFactory? backendFactory;

  /// Start with the wide sidebar collapsed to its icon rail (remembered in
  /// [ShellPreferences]).
  final bool initialSidebarCollapsed;

  /// Tests: stand-in destination pages.
  @visibleForTesting
  final ShellPageBuilder? pageBuilder;

  /// Tests: the approvals ledger the Runtime badge counts.
  @visibleForTesting
  final ApprovalsReader? approvals;

  /// Tests: the clock the conversation list groups dates by.
  @visibleForTesting
  final DateTime Function()? clock;

  const AppShell({
    super.key,
    required this.settings,
    required this.onSettingsChanged,
    this.backendFactory,
    this.initialSidebarCollapsed = false,
    this.pageBuilder,
    this.approvals,
    this.clock,
  });

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell>
    with WidgetsBindingObserver, TickerProviderStateMixin {
  late final ChatSession _session;
  late final ApprovalsWatch _approvals;

  /// 1 = sidebar expanded, 0 = icon rail.
  late final AnimationController _rail;

  /// The Chat layer's opacity: it fades out (and goes offstage) while
  /// another destination is shown.
  late final AnimationController _chatFade;

  final _scaffoldKey = GlobalKey<ScaffoldState>();

  /// The shell's own focus: the keyboard lands here when nothing inside a
  /// page holds it, so the shortcuts always reach the shell.
  final _shellFocus = FocusNode(debugLabel: 'shell', skipTraversal: true);
  final _contentKey = GlobalKey(debugLabel: 'shell-content');
  final _chatNavigatorKey = GlobalKey<NavigatorState>();
  GlobalKey<NavigatorState> _pageNavigatorKey = GlobalKey<NavigatorState>();

  final Map<WorkspaceDestination, ShellLeaveGuards> _guards = {
    for (final d in WorkspaceDestination.values) d: ShellLeaveGuards(),
  };

  final _sidebarChat = ValueNotifier<SidebarChatState>(SidebarChatState.empty);
  final _badges = ValueNotifier<Map<WorkspaceDestination, SidebarBadge>>(
      const <WorkspaceDestination, SidebarBadge>{});

  WorkspaceDestination _current = WorkspaceDestination.chat;
  ShellSection? _section;
  int _pageSerial = 0;
  late bool _collapsed = widget.initialSidebarCollapsed;
  bool _leaving = false;
  bool _chatHasRoutes = false;
  bool _wide = true;
  String? _projectFilter;
  int _identityGeneration = 0;

  DateTime _now() => widget.clock?.call() ?? DateTime.now();

  @override
  void initState() {
    super.initState();
    _rail = AnimationController(
      vsync: this,
      duration: SonderMotion.medium,
      value: _collapsed ? 0 : 1,
    );
    _chatFade = AnimationController(
      vsync: this,
      duration: SonderMotion.medium,
      reverseDuration: SonderMotion.fast,
      value: 1,
    );
    _session = ChatSession(
      settings: widget.settings,
      backendFactory: widget.backendFactory,
    );
    _identityGeneration = _session.identityGeneration;
    _session.chat.onAnnounce = (message) {
      if (mounted) announceToScreenReader(context, message);
    };
    _session.chat.addListener(_chatChanged);
    _session.chat.status.addListener(_updateBadges);
    _approvals = ApprovalsWatch(
      read: widget.approvals ?? () => _session.api.approvals.list(limit: 50),
      connection: _session.chat.connection,
    );
    _approvals.waiting.addListener(_updateBadges);
    WidgetsBinding.instance.addObserver(this);
    _session.start();
    _approvals.start();
    _chatChanged();
  }

  @override
  void didUpdateWidget(covariant AppShell oldWidget) {
    super.didUpdateWidget(oldWidget);
    _session.syncSettings(widget.settings);
    _identityChanged();
  }

  void _identityChanged() {
    if (_session.identityGeneration == _identityGeneration) return;
    _identityGeneration = _session.identityGeneration;
    _approvals.reset();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _session.handleLifecycle(state);
    switch (state) {
      case AppLifecycleState.resumed:
        _approvals.resume();
      case AppLifecycleState.paused ||
            AppLifecycleState.hidden ||
            AppLifecycleState.detached:
        _approvals.pause();
      case AppLifecycleState.inactive:
        break;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _session.chat.removeListener(_chatChanged);
    _session.chat.status.removeListener(_updateBadges);
    _approvals.waiting.removeListener(_updateBadges);
    _approvals.dispose();
    _session.dispose();
    _shellFocus.dispose();
    _rail.dispose();
    _chatFade.dispose();
    _sidebarChat.dispose();
    _badges.dispose();
    super.dispose();
  }

  // -- Chat state for the sidebar ------------------------------------------

  void _chatChanged() {
    final chat = _session.chat;
    _sidebarChat.value = SidebarChatState(
      threads: chat.threads,
      currentThreadId: chat.currentThreadId,
      runningThreadId: chat.turnThreadId,
      loading: chat.loadingThreads,
    );
    _updateBadges();
  }

  void _updateBadges() {
    final chat = _session.chat;
    final badges = <WorkspaceDestination, SidebarBadge>{};
    if (chat.sending && _current != WorkspaceDestination.chat) {
      badges[WorkspaceDestination.chat] = SidebarBadge(
        kind: StatusKind.running,
        text: StatusKind.running.word,
        semantic: 'a reply is ${StatusKind.running.word}',
        mini: StatusKind.running.glyph,
      );
    }
    final info = chat.status.value;
    final running =
        (info?.agents?.activeAgents ?? 0) + (info?.autopilot?.activeRuns ?? 0);
    if (running > 0) {
      badges[WorkspaceDestination.agents] = SidebarBadge(
        kind: StatusKind.running,
        text: '$running',
        semantic: '$running running',
        mini: '$running',
      );
    }
    final waiting = _approvals.waiting.value ?? 0;
    if (waiting > 0) {
      badges[WorkspaceDestination.runtime] = SidebarBadge(
        kind: StatusKind.warn,
        text: '$waiting',
        semantic: '$waiting ${waiting == 1 ? 'approval' : 'approvals'} '
            'waiting',
        mini: '$waiting',
      );
    }
    if (!mapEquals(badges, _badges.value)) _badges.value = badges;
  }

  // -- Navigation -----------------------------------------------------------

  /// Switch to [destination] (at [section]), after the current page's leave
  /// guards agree.
  Future<void> _navigate(WorkspaceDestination destination,
      {String? section, Map<String, String> params = const {}}) async {
    if (!mounted) return;
    _closeDrawer();
    if (destination == _current) {
      if (section != null) {
        setState(() => _section = ShellSection(section, params: params));
      }
      if (destination == WorkspaceDestination.chat) _focusComposerSoon();
      return;
    }
    if (_leaving) return;
    _leaving = true;
    var leave = false;
    try {
      leave = await _guards[_current]!.canLeave();
    } finally {
      _leaving = false;
    }
    if (!leave || !mounted) return;
    final left = _current;
    setState(() {
      _current = destination;
      _section = section == null ? null : ShellSection(section, params: params);
      if (destination != WorkspaceDestination.chat) {
        _pageSerial++;
        _pageNavigatorKey = GlobalKey<NavigatorState>();
      }
    });
    // Settings may have edited the shared settings object in place.
    if (left == WorkspaceDestination.settings) {
      _session.syncSettings(widget.settings);
      _identityChanged();
    }
    _syncChatLayer();
    _updateBadges();
    _keepKeyboardInShell();
    if (destination == WorkspaceDestination.chat) _focusComposerSoon();
  }

  /// A page that held the keyboard focus (the composer, a field) may have
  /// just gone; if focus fell out above the shell, take it, so shortcuts
  /// keep working. Focus inside a dialog is left alone.
  void _keepKeyboardInShell() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final primary = FocusManager.instance.primaryFocus;
      if (primary == null || _shellFocus.ancestors.contains(primary)) {
        _shellFocus.requestFocus();
      }
    });
  }

  void _openSection(WorkspaceDestination destination, String section,
          {Map<String, String> params = const {}}) =>
      unawaited(_navigate(destination, section: section, params: params));

  void _syncChatLayer() {
    _chatFade
      ..duration = SonderMotion.of(context, SonderMotion.medium)
      ..reverseDuration = SonderMotion.of(context, SonderMotion.fast);
    if (_current == WorkspaceDestination.chat) {
      unawaited(_chatFade.forward());
    } else {
      unawaited(_chatFade.reverse());
    }
  }

  /// On a pointer layout, put the caret back in the composer once Chat is
  /// focusable again. Phones keep the keyboard closed.
  void _focusComposerSoon() {
    if (!_wide) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && _current == WorkspaceDestination.chat) {
        _session.page?.focusComposer();
      }
    });
  }

  void _closeDrawer() {
    final scaffold = _scaffoldKey.currentState;
    if (scaffold != null && scaffold.isDrawerOpen) scaffold.closeDrawer();
  }

  void _openNavigation() {
    if (_wide) {
      if (_collapsed) _toggleSidebar();
      return;
    }
    _scaffoldKey.currentState?.openDrawer();
  }

  void _toggleSidebar() {
    if (!_wide) {
      final scaffold = _scaffoldKey.currentState;
      if (scaffold == null) return;
      if (scaffold.isDrawerOpen) {
        scaffold.closeDrawer();
      } else {
        scaffold.openDrawer();
      }
      return;
    }
    setState(() => _collapsed = !_collapsed);
    _rail.duration = SonderMotion.of(context, SonderMotion.medium);
    if (_collapsed) {
      unawaited(_rail.reverse());
    } else {
      unawaited(_rail.forward());
    }
    unawaited(ShellPreferences.setSidebarCollapsed(_collapsed));
  }

  /// System back: a page's own routes first, then back to Chat.
  Future<void> _handleBack() async {
    final navigator = _current == WorkspaceDestination.chat
        ? _chatNavigatorKey.currentState
        : _pageNavigatorKey.currentState;
    if (navigator != null && await navigator.maybePop()) return;
    if (_current != WorkspaceDestination.chat) {
      await _navigate(WorkspaceDestination.chat);
    }
  }

  // -- Chat actions -----------------------------------------------------------

  Future<void> _newChat() async {
    await _navigate(WorkspaceDestination.chat);
    if (!mounted || _current != WorkspaceDestination.chat) return;
    final chat = _session.chat;
    // An untouched new chat is already open: reuse it rather than pile up
    // empty conversations.
    final untouched = chat.entries.isEmpty &&
        !chat.sending &&
        chat.currentThread.messages.isEmpty;
    if (!untouched) chat.newChat();
    _focusComposerSoon();
  }

  Future<void> _openThread(ChatThread thread) async {
    await _navigate(WorkspaceDestination.chat);
    if (!mounted || _current != WorkspaceDestination.chat) return;
    _session.chat.switchThread(thread);
  }

  Future<void> _openSearch() async {
    _closeDrawer();
    final chat = _session.chat;
    final picked = await showThreadSwitcher(
      context,
      threads: chat.threads,
      currentThreadId: chat.currentThreadId,
      now: _now(),
    );
    if (picked != null && mounted) await _openThread(picked);
  }

  Future<void> _openCommands() async {
    await _navigate(WorkspaceDestination.chat);
    if (!mounted || _current != WorkspaceDestination.chat) return;
    _session.page?.openCommandBrowser();
  }

  /// Deletes at once; a chat with messages gets an Undo that restores it.
  Future<void> _deleteThread(ChatThread thread) async {
    final hadMessages = thread.messages.isNotEmpty;
    final chat = _session.chat;
    final deleted = await chat.deleteThread(thread);
    if (!mounted || !hadMessages) return;
    showSonderToast(
      context,
      'Chat deleted.',
      kind: StatusKind.note,
      actionLabel: 'Undo',
      onAction: () => unawaited(chat.restoreThread(deleted)),
      duration: const Duration(seconds: 6),
    );
  }

  void _openConnection() {
    final c = _session.chat.connection.value;
    if (c.isConnected || c.state == ConnState.connecting) {
      unawaited(_navigate(WorkspaceDestination.runtime));
    } else {
      _openSection(WorkspaceDestination.settings, 'connection');
    }
  }

  void _settingsSaved(Settings next) {
    _session.settingsSaved(next);
    widget.onSettingsChanged(next);
  }

  // -- Layout ---------------------------------------------------------------

  ShellScope _scoped(WorkspaceDestination destination, Widget child) =>
      ShellScope(
        current: _current,
        sidebarVisible: _wide,
        navigate: (d) => unawaited(_navigate(d)),
        openNavigation: _openNavigation,
        openSection: _openSection,
        section: destination == _current ? _section : null,
        leaveGuards: _guards[destination],
        child: child,
      );

  Widget _page(WorkspaceDestination destination) {
    final custom = widget.pageBuilder?.call(context, destination, _section);
    if (custom != null) return custom;
    void navigate(WorkspaceDestination d) => unawaited(_navigate(d));
    return switch (destination) {
      WorkspaceDestination.agents => AgentScreen(
          api: _session.api,
          initialLaneId: _section?.id,
          initialProject: _session.chat.project,
          onNavigate: navigate,
        ),
      WorkspaceDestination.runtime =>
        SystemScreen(settings: widget.settings, onNavigate: navigate),
      WorkspaceDestination.settings => SettingsScreen(
          settings: widget.settings,
          onChanged: _settingsSaved,
          onNavigate: navigate,
        ),
      WorkspaceDestination.chat => const SizedBox.shrink(),
    };
  }

  Widget _content() {
    final chat = _scoped(
      WorkspaceDestination.chat,
      ShellPageNavigator(
        navigatorKey: _chatNavigatorKey,
        leaveOnBack: false,
        onLeaveRequested: () {},
        onHasRoutesChanged: (hasRoutes) {
          if (mounted) setState(() => _chatHasRoutes = hasRoutes);
        },
        child: ChatScreen(
          settings: widget.settings,
          onSettingsChanged: widget.onSettingsChanged,
          backendFactory: widget.backendFactory,
          session: _session,
        ),
      ),
    );
    final other = _current == WorkspaceDestination.chat
        ? null
        : KeyedSubtree(
            key: ValueKey('page-${_current.name}-$_pageSerial'),
            child: _scoped(
              _current,
              ShellPageNavigator(
                navigatorKey: _pageNavigatorKey,
                onLeaveRequested: () =>
                    unawaited(_navigate(WorkspaceDestination.chat)),
                child: _page(_current),
              ),
            ),
          );
    return Stack(
      fit: StackFit.expand,
      children: [
        _ChatLayer(
          opacity: _chatFade,
          visible: _current == WorkspaceDestination.chat,
          child: chat,
        ),
        _FadeThrough(child: other),
      ],
    );
  }

  Widget _sidebar({required bool inDrawer}) => ShellSidebar(
        key: ValueKey(inDrawer ? 'drawer-sidebar' : 'rail-sidebar'),
        expansion: _rail,
        collapsed: _collapsed,
        inDrawer: inDrawer,
        current: _current,
        chat: _sidebarChat,
        badges: _badges,
        connection: _session.chat.connection,
        now: _now,
        projectFilter: _projectFilter,
        onProjectFilter: (p) => setState(() => _projectFilter = p),
        onNewChat: () => unawaited(_newChat()),
        onSearch: () => unawaited(_openSearch()),
        onToggleCollapsed: inDrawer ? null : _toggleSidebar,
        onShowShortcuts: () => unawaited(showShortcutGuide(context)),
        onDestination: (d) => unawaited(_navigate(d)),
        onOpenThread: (t) => unawaited(_openThread(t)),
        onDeleteThread: (t) => unawaited(_deleteThread(t)),
        onConnection: _openConnection,
      );

  Map<Type, Action<Intent>> get _actions => <Type, Action<Intent>>{
        NewChatIntent: CallbackAction<NewChatIntent>(
            onInvoke: (_) => unawaited(_newChat())),
        OpenCommandsIntent: CallbackAction<OpenCommandsIntent>(
            onInvoke: (_) => unawaited(_openCommands())),
        SearchChatsIntent: CallbackAction<SearchChatsIntent>(
            onInvoke: (_) => unawaited(_openSearch())),
        GoToDestinationIntent: CallbackAction<GoToDestinationIntent>(
            onInvoke: (intent) => unawaited(_navigate(intent.destination))),
        ToggleSidebarIntent: CallbackAction<ToggleSidebarIntent>(
            onInvoke: (_) => _toggleSidebar()),
        ShowShortcutsIntent: CallbackAction<ShowShortcutsIntent>(
            onInvoke: (_) => unawaited(showShortcutGuide(context))),
      };

  @override
  Widget build(BuildContext context) {
    final platform = Theme.of(context).platform;
    return LayoutBuilder(builder: (context, constraints) {
      // Read by callbacks; the drawer leaves with the narrow layout.
      final wide = _wide = constraints.maxWidth >= kShellWideBreakpoint;
      final content = KeyedSubtree(key: _contentKey, child: _content());
      return PopScope<Object?>(
        canPop: _current == WorkspaceDestination.chat && !_chatHasRoutes,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop) unawaited(_handleBack());
        },
        child: Shortcuts(
          shortcuts: shellShortcuts(platform),
          child: Actions(
            actions: _actions,
            child: Focus(
              focusNode: _shellFocus,
              autofocus: true,
              child: Scaffold(
                key: _scaffoldKey,
                drawer: wide
                    ? null
                    : Drawer(
                        width: 304,
                        child: _sidebar(inDrawer: true),
                      ),
                drawerEnableOpenDragGesture: !wide,
                body: wide
                    ? Row(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          _sidebar(inDrawer: false),
                          Expanded(child: content),
                        ],
                      )
                    : content,
              ),
            ),
          ),
        ),
      );
    });
  }
}

/// The always-mounted Chat page. While another destination is shown it
/// fades out, then goes offstage with its tickers paused and its focus,
/// pointer and semantics withdrawn; the widget state (and the composer
/// draft) stays.
class _ChatLayer extends StatelessWidget {
  final Animation<double> opacity;
  final bool visible;
  final Widget child;

  const _ChatLayer({
    required this.opacity,
    required this.visible,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: opacity,
      child: child,
      builder: (context, child) {
        final gone = !visible && opacity.value == 0;
        return Offstage(
          offstage: gone,
          child: TickerMode(
            enabled: visible,
            child: ExcludeFocus(
              excluding: !visible,
              child: IgnorePointer(
                ignoring: !visible,
                child: Opacity(opacity: opacity.value, child: child),
              ),
            ),
          ),
        );
      },
    );
  }
}

/// Destinations other than Chat swap with a short fade-through: the old
/// page fades out quickly, the new one fades in with a slight scale.
class _FadeThrough extends StatelessWidget {
  final Widget? child;
  const _FadeThrough({required this.child});

  @override
  Widget build(BuildContext context) {
    return AnimatedSwitcher(
      duration: SonderMotion.of(context, SonderMotion.medium),
      reverseDuration: SonderMotion.of(context, SonderMotion.fast),
      switchInCurve: SonderMotion.enter,
      switchOutCurve: SonderMotion.exit,
      layoutBuilder: (current, previous) => Stack(
        fit: StackFit.expand,
        children: [...previous, if (current != null) current],
      ),
      transitionBuilder: (child, animation) {
        final scale = Tween<double>(begin: 0.985, end: 1).animate(animation);
        return AnimatedBuilder(
          animation: animation,
          child: child,
          builder: (context, child) => IgnorePointer(
            ignoring: animation.status == AnimationStatus.reverse,
            child: FadeTransition(
              opacity: animation,
              child: ScaleTransition(scale: scale, child: child),
            ),
          ),
        );
      },
      child: child,
    );
  }
}
