import 'dart:ui' show lerpDouble;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../chat/connection.dart';
import '../chat/drawer.dart';
import '../models.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/sonder_mark.dart';
import '../workspace_ui.dart';
import 'shortcuts.dart';

/// Width of the expanded sidebar.
const double kSidebarWidth = 264;

/// Width of the collapsed icon rail.
const double kSidebarRailWidth = 64;

/// The square every rail item collapses to.
const double _itemSize = 48;

/// Rows sit this far in from the sidebar's edges.
const double _inset = SonderSpace.sm;

/// The icon slot: centred on the rail in both states.
const double _slot = 24;

/// A row's hit area is 48 px; the pill drawn for hover and selection is
/// inset so rows read as a list rather than a stack of slabs.
const EdgeInsets _pillInset = EdgeInsets.symmetric(vertical: SonderSpace.xs);

/// A count or status shown beside a destination. It always carries words:
/// [text] is drawn with the kind's glyph, [semantic] is what is heard.
@immutable
class SidebarBadge {
  final StatusKind kind;
  final String text;
  final String semantic;

  /// What the collapsed rail draws on the icon's corner ("2", "◈").
  final String mini;

  const SidebarBadge({
    required this.kind,
    required this.text,
    required this.semantic,
    required this.mini,
  });

  @override
  bool operator ==(Object other) =>
      other is SidebarBadge &&
      other.kind == kind &&
      other.text == text &&
      other.semantic == semantic &&
      other.mini == mini;

  @override
  int get hashCode => Object.hash(kind, text, semantic, mini);
}

/// The chat facts the thread list draws, compared cheaply so a streaming
/// delta (which changes none of them) does not rebuild the list.
@immutable
class SidebarChatState {
  final List<ChatThread> threads;
  final String currentThreadId;
  final String? runningThreadId;
  final bool loading;

  const SidebarChatState({
    required this.threads,
    required this.currentThreadId,
    required this.runningThreadId,
    required this.loading,
  });

  static const empty = SidebarChatState(
      threads: [], currentThreadId: '', runningThreadId: null, loading: true);

  @override
  bool operator ==(Object other) =>
      other is SidebarChatState &&
      identical(other.threads, threads) &&
      other.currentThreadId == currentThreadId &&
      other.runningThreadId == runningThreadId &&
      other.loading == loading;

  @override
  int get hashCode => Object.hash(
      identityHashCode(threads), currentThreadId, runningThreadId, loading);
}

/// The app's persistent navigation, in the order people know from chat
/// apps: brand, New chat, Search, then the conversations grouped by date
/// under a "Chats" row that is also the way back to the conversation; the
/// working destinations (Agents, Runtime, Settings) with their badges sit
/// below the list, above the connection footer.
///
/// On wide windows it sits beside the page and collapses to a 64 px icon
/// rail ([expansion] runs 1 → 0); the layout is drawn at full width and
/// clipped, so icons stay put while labels fade. On narrow windows it is
/// the content of the navigation drawer ([inDrawer]).
class ShellSidebar extends StatelessWidget {
  final Animation<double> expansion;

  /// The state [expansion] is heading to.
  final bool collapsed;
  final bool inDrawer;
  final WorkspaceDestination current;
  final ValueListenable<SidebarChatState> chat;
  final ValueListenable<Map<WorkspaceDestination, SidebarBadge>> badges;
  final ValueListenable<ConnectionStatus> connection;
  final DateTime Function() now;

  /// Only conversations in this project; null for all.
  final String? projectFilter;
  final ValueChanged<String?> onProjectFilter;

  final VoidCallback onNewChat;
  final VoidCallback onSearch;
  final VoidCallback? onToggleCollapsed;
  final VoidCallback? onShowShortcuts;
  final ValueChanged<WorkspaceDestination> onDestination;
  final ValueChanged<ChatThread> onOpenThread;
  final ValueChanged<ChatThread> onDeleteThread;
  final VoidCallback onConnection;

  const ShellSidebar({
    super.key,
    required this.expansion,
    required this.collapsed,
    required this.current,
    required this.chat,
    required this.badges,
    required this.connection,
    required this.now,
    required this.projectFilter,
    required this.onProjectFilter,
    required this.onNewChat,
    required this.onSearch,
    required this.onDestination,
    required this.onOpenThread,
    required this.onDeleteThread,
    required this.onConnection,
    this.onToggleCollapsed,
    this.onShowShortcuts,
    this.inDrawer = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    if (inDrawer) {
      return ColoredBox(
        color: tokens.sunken,
        child: SafeArea(
          right: false,
          child: LayoutBuilder(
            builder: (context, constraints) =>
                _content(context, 1, constraints.maxWidth),
          ),
        ),
      );
    }
    return AnimatedBuilder(
      animation: expansion,
      builder: (context, _) {
        final t = SonderMotion.standard.transform(expansion.value);
        final width = lerpDouble(kSidebarRailWidth, kSidebarWidth, t)!;
        return Container(
          key: const Key('shell-sidebar'),
          width: width,
          // The frame is told from the page by tone alone.
          color: tokens.sunken,
          child: ClipRect(
            child: OverflowBox(
              alignment: Alignment.topLeft,
              minWidth: kSidebarWidth,
              maxWidth: kSidebarWidth,
              child: _content(context, t, kSidebarWidth),
            ),
          ),
        );
      },
    );
  }

  Widget _content(BuildContext context, double t, double width) {
    final tokens = SonderTokens.of(context);
    final keys = ShortcutKeys(Theme.of(context).platform);
    final hidden = collapsed && !inDrawer;
    return _Expansion(
      t: t,
      fullWidth: width - 2 * _inset,
      collapsed: hidden,
      child: FocusTraversalGroup(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _BrandRow(
              collapsed: hidden,
              inDrawer: inDrawer,
              onToggle: onToggleCollapsed,
              toggleTooltip: hidden
                  ? 'Expand sidebar (${keys.combo('B')})'
                  : 'Collapse sidebar (${keys.combo('B')})',
              onShowShortcuts: inDrawer ? null : onShowShortcuts,
              shortcutsTooltip: 'Keyboard shortcuts (${keys.combo('/')})',
            ),
            const SizedBox(height: SonderSpace.xs),
            _NavItem(
              key: const Key('shell-new-chat'),
              icon: Icon(Icons.maps_ugc_outlined, size: 18, color: tokens.text),
              label: 'New chat',
              emphasis: true,
              tooltip: 'New chat (${keys.combo('N')})',
              onTap: onNewChat,
            ),
            _NavItem(
              key: const Key('shell-search'),
              icon: Icon(Icons.search, size: 18, color: tokens.text2),
              label: 'Search chats',
              tooltip: 'Search chats (${keys.combo('P')})',
              onTap: onSearch,
            ),
            const SizedBox(height: SonderSpace.sm),
            ValueListenableBuilder<SidebarChatState>(
              valueListenable: chat,
              builder: (context, state, _) => ValueListenableBuilder<
                  Map<WorkspaceDestination, SidebarBadge>>(
                valueListenable: badges,
                builder: (context, badges, _) => _ChatsRow(
                  selected: current == WorkspaceDestination.chat,
                  // A reply still being written while another page is up.
                  badge: badges[WorkspaceDestination.chat],
                  tooltip: '${WorkspaceDestination.chat.label} '
                      '(${keys.combo('${destinationNumber(WorkspaceDestination.chat)}')})',
                  onTap: () => onDestination(WorkspaceDestination.chat),
                  projects: _projectsOf(state.threads),
                  projectFilter: projectFilter,
                  onProjectFilter: onProjectFilter,
                ),
              ),
            ),
            Expanded(
              child: _Fade(
                child: ValueListenableBuilder<SidebarChatState>(
                  valueListenable: chat,
                  builder: (context, state, _) => _ThreadSection(
                    state: state,
                    touch: inDrawer,
                    chatShown: current == WorkspaceDestination.chat,
                    now: now(),
                    projectFilter: projectFilter,
                    onOpenThread: onOpenThread,
                    onDeleteThread: onDeleteThread,
                  ),
                ),
              ),
            ),
            // The list scrolls under this edge; the rule marks where.
            Divider(height: 1, thickness: 1, color: tokens.hairline),
            const SizedBox(height: SonderSpace.xs),
            ValueListenableBuilder<Map<WorkspaceDestination, SidebarBadge>>(
              valueListenable: badges,
              builder: (context, badges, _) => Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final destination in WorkspaceDestination.values)
                    if (destination != WorkspaceDestination.chat)
                      _NavItem(
                        key: Key('shell-destination-${destination.name}'),
                        icon: Icon(destination.icon,
                            size: 18,
                            color: destination == current
                                ? tokens.text
                                : tokens.text2),
                        label: destination.label,
                        selected: destination == current,
                        badge: badges[destination],
                        tooltip: '${destination.label} '
                            '(${keys.combo('${destinationNumber(destination)}')})',
                        onTap: () => onDestination(destination),
                      ),
                ],
              ),
            ),
            _Footer(connection: connection, onConnection: onConnection),
          ],
        ),
      ),
    );
  }
}

/// The projects [threads] span, sorted; an unnamed project is "default".
List<String> _projectsOf(List<ChatThread> threads) => {
      for (final t in threads) t.project.trim().isEmpty ? 'default' : t.project,
    }.toList()
      ..sort();

/// The expansion state, read by every row: [t] runs 0 (rail) to 1 (full).
class _Expansion extends InheritedWidget {
  final double t;
  final double fullWidth;
  final bool collapsed;

  const _Expansion({
    required this.t,
    required this.fullWidth,
    required this.collapsed,
    required super.child,
  });

  static _Expansion of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<_Expansion>()!;

  /// A row's width: a square on the rail, [full] (default: the whole row)
  /// when expanded.
  double itemWidth({double? full}) =>
      lerpDouble(_itemSize, full ?? fullWidth, t)!;

  @override
  bool updateShouldNotify(_Expansion oldWidget) =>
      t != oldWidget.t ||
      fullWidth != oldWidget.fullWidth ||
      collapsed != oldWidget.collapsed;
}

/// How visible expanded-only content is at expansion [t]. Labels fade over
/// the second half of an expansion and the first half of a collapse, so
/// nothing crosses the clip edge at full strength.
double _fadeOf(double t) => ((t - 0.4) / 0.6).clamp(0.0, 1.0);

/// Fades content that only exists in the expanded sidebar; on the rail it
/// is gone from hit testing, focus and semantics too.
class _Fade extends StatelessWidget {
  final Widget child;
  const _Fade({required this.child});

  @override
  Widget build(BuildContext context) {
    final e = _Expansion.of(context);
    final gone = e.collapsed;
    final opacity = _fadeOf(e.t);
    return IgnorePointer(
      ignoring: gone,
      child: ExcludeFocus(
        excluding: gone,
        child: ExcludeSemantics(
          excluding: gone,
          child: Opacity(opacity: opacity, child: child),
        ),
      ),
    );
  }
}

class _BrandRow extends StatelessWidget {
  final bool collapsed;
  final bool inDrawer;
  final VoidCallback? onToggle;
  final String toggleTooltip;
  final VoidCallback? onShowShortcuts;
  final String shortcutsTooltip;

  const _BrandRow({
    required this.collapsed,
    required this.inDrawer,
    required this.onToggle,
    required this.toggleTooltip,
    required this.onShowShortcuts,
    required this.shortcutsTooltip,
  });

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final tokens = SonderTokens.of(context);
    final toggle = onToggle;
    return SizedBox(
      height: 48,
      child: Row(children: [
        const SizedBox(width: _inset),
        SizedBox.square(
          dimension: _itemSize,
          child: collapsed && toggle != null
              ? _ExpandButton(onPressed: toggle, tooltip: toggleTooltip)
              : const Center(child: SonderMark(size: 22)),
        ),
        Expanded(
          child: _Fade(
            child: Row(children: [
              Expanded(
                child: Semantics(
                  header: true,
                  child: Text('Sonder',
                      maxLines: 1,
                      overflow: TextOverflow.clip,
                      softWrap: false,
                      style: text.titleSmall?.copyWith(
                          fontWeight: FontWeight.w600, color: tokens.text)),
                ),
              ),
              if (onShowShortcuts case final show?)
                IconButton(
                  key: const Key('shell-shortcuts'),
                  tooltip: shortcutsTooltip,
                  onPressed: show,
                  icon: Icon(Icons.keyboard_outlined,
                      size: 20, color: tokens.text2),
                ),
              if (!inDrawer && toggle != null)
                IconButton(
                  key: const Key('shell-collapse'),
                  tooltip: toggleTooltip,
                  onPressed: toggle,
                  icon: const _SidebarIcon(),
                ),
              const SizedBox(width: SonderSpace.xs),
            ]),
          ),
        ),
      ]),
    );
  }
}

/// The rail's top cell: the mark, which turns into the expand control
/// under the pointer or keyboard focus.
class _ExpandButton extends StatefulWidget {
  final VoidCallback onPressed;
  final String tooltip;
  const _ExpandButton({required this.onPressed, required this.tooltip});

  @override
  State<_ExpandButton> createState() => _ExpandButtonState();
}

class _ExpandButtonState extends State<_ExpandButton> {
  bool _active = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      excludeFromSemantics: true,
      child: Semantics(
        container: true,
        button: true,
        label: 'Expand sidebar',
        excludeSemantics: true,
        child: MouseRegion(
          onEnter: (_) => setState(() => _active = true),
          onExit: (_) => setState(() => _active = false),
          child: Material(
            type: MaterialType.transparency,
            child: InkWell(
              key: const Key('shell-expand'),
              borderRadius: BorderRadius.circular(SonderRadius.row),
              onTap: widget.onPressed,
              onFocusChange: (f) => setState(() => _active = f),
              child: Center(
                child: AnimatedSwitcher(
                  duration: SonderMotion.of(context, SonderMotion.fast),
                  child: _active
                      ? const _SidebarIcon(key: ValueKey('icon'))
                      : const SonderMark(key: ValueKey('mark'), size: 22),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// A left-docked panel glyph.
class _SidebarIcon extends StatelessWidget {
  const _SidebarIcon({super.key});

  @override
  Widget build(BuildContext context) => Transform.flip(
        flipX: true,
        child: Icon(Icons.view_sidebar_outlined,
            size: 20, color: SonderTokens.of(context).text2),
      );
}

/// One rail entry: an icon slot centred on the rail, a label and an
/// optional badge that fade in as the sidebar expands. The selected entry
/// is a soft pill of the raised tone, as in the category rails.
class _NavItem extends StatelessWidget {
  final Widget icon;
  final String label;
  final String tooltip;
  final VoidCallback onTap;
  final bool selected;
  final bool emphasis;
  final SidebarBadge? badge;

  const _NavItem({
    super.key,
    required this.icon,
    required this.label,
    required this.tooltip,
    required this.onTap,
    this.selected = false,
    this.emphasis = false,
    this.badge,
  });

  @override
  Widget build(BuildContext context) {
    final e = _Expansion.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: _inset),
      child: Align(
        alignment: Alignment.centerLeft,
        child: SizedBox(
          width: e.itemWidth(),
          child: _NavSurface(
            icon: icon,
            label: label,
            tooltip: tooltip,
            onTap: onTap,
            selected: selected,
            emphasis: emphasis,
            badge: badge,
          ),
        ),
      ),
    );
  }
}

/// The tappable body of a rail entry, as wide as its parent allows.
class _NavSurface extends StatelessWidget {
  final Widget icon;
  final String label;
  final String tooltip;
  final VoidCallback onTap;
  final bool selected;
  final bool emphasis;
  final SidebarBadge? badge;

  /// Draw the selection only on the collapsed rail: expanded, something
  /// else (the open conversation's row) already shows where the person is.
  final bool quietWhenExpanded;

  const _NavSurface({
    required this.icon,
    required this.label,
    required this.tooltip,
    required this.onTap,
    this.selected = false,
    this.emphasis = false,
    this.badge,
    this.quietWhenExpanded = false,
  });

  @override
  Widget build(BuildContext context) {
    final e = _Expansion.of(context);
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final badge = this.badge;
    final semantic = badge == null ? label : '$label, ${badge.semantic}';
    final labelStyle = text.bodyMedium?.copyWith(
      color: tokens.text,
      fontWeight: selected || emphasis ? FontWeight.w500 : FontWeight.w400,
    );
    return Tooltip(
      message: tooltip,
      excludeFromSemantics: true,
      // One node per entry, so its label and state are its own.
      child: Semantics(
        container: true,
        child: HoverSurface(
          selected: selected,
          showSelected: !quietWhenExpanded || e.collapsed,
          onTap: onTap,
          semanticLabel: semantic,
          inset: _pillInset,
          child: ExcludeSemantics(
            child: SizedBox(
              height: _itemSize - _pillInset.vertical,
              child: Stack(clipBehavior: Clip.hardEdge, children: [
                // The content is laid out at the full row width and
                // clipped, so labels never re-wrap mid-animation.
                Positioned(
                  left: 0,
                  top: 0,
                  bottom: 0,
                  width: e.fullWidth,
                  child: Row(children: [
                    const SizedBox(width: SonderSpace.md),
                    SizedBox(
                      width: _slot,
                      child: Center(child: icon),
                    ),
                    const SizedBox(width: SonderSpace.md),
                    Expanded(
                      child: _Fade(
                        child: Row(children: [
                          Expanded(
                            child: Text(label,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                softWrap: false,
                                style: labelStyle),
                          ),
                          if (badge != null) ...[
                            const SizedBox(width: SonderSpace.sm),
                            StatusPill(badge.kind,
                                word: badge.text, dense: true),
                          ],
                          const SizedBox(width: SonderSpace.md),
                        ]),
                      ),
                    ),
                  ]),
                ),
                if (badge != null)
                  Positioned(
                    left: SonderSpace.md + _slot - SonderSpace.sm,
                    top: SonderSpace.xs,
                    child: Opacity(
                      opacity: (1 - e.t * 2).clamp(0.0, 1.0),
                      child: _MiniBadge(badge),
                    ),
                  ),
              ]),
            ),
          ),
        ),
      ),
    );
  }
}

/// The badge on a rail icon's corner: a solid chip with the count or glyph.
class _MiniBadge extends StatelessWidget {
  final SidebarBadge badge;
  const _MiniBadge(this.badge);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final color = badge.kind.color(tokens);
    return Container(
      constraints: const BoxConstraints(minWidth: 16, minHeight: 16),
      padding: const EdgeInsets.symmetric(horizontal: SonderSpace.xs),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(SonderRadius.pill),
        border: Border.all(color: tokens.sunken, width: 2),
      ),
      alignment: Alignment.center,
      child: Text(badge.mini,
          style:
              tokens.mono(10, color: tokens.canvas, weight: FontWeight.w600)),
    );
  }
}

class _ThreadSection extends StatelessWidget {
  final SidebarChatState state;
  final bool chatShown;
  final bool touch;
  final DateTime now;
  final String? projectFilter;
  final ValueChanged<ChatThread> onOpenThread;
  final ValueChanged<ChatThread> onDeleteThread;

  const _ThreadSection({
    required this.state,
    required this.chatShown,
    required this.touch,
    required this.now,
    required this.projectFilter,
    required this.onOpenThread,
    required this.onDeleteThread,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final projects = _projectsOf(state.threads);
    final filter = projects.contains(projectFilter) ? projectFilter : null;
    final shown = filter == null
        ? state.threads
        : [
            for (final t in state.threads)
              if (t.project == filter) t,
          ];
    final children = <Widget>[];
    if (state.loading) {
      children.add(const ThreadRowsSkeleton());
    } else if (shown.isEmpty) {
      children.add(Padding(
        padding: const EdgeInsets.fromLTRB(_inset + SonderSpace.md,
            SonderSpace.md, _inset + SonderSpace.md, SonderSpace.md),
        child: Text(filter == null ? 'No chats yet' : 'No chats in $filter',
            style: text.bodySmall?.copyWith(color: tokens.muted)),
      ));
    } else {
      for (final (age, threads) in groupThreads(shown, now)) {
        children.add(Padding(
          key: ValueKey('thread-group-${age.name}'),
          padding: const EdgeInsets.fromLTRB(_inset + SonderSpace.md,
              SonderSpace.md, _inset + SonderSpace.md, SonderSpace.xs),
          child: Semantics(
            header: true,
            child: Text(age.label,
                style: text.labelMedium?.copyWith(color: tokens.muted)),
          ),
        ));
        for (final thread in threads) {
          children.add(Padding(
            padding: const EdgeInsets.symmetric(horizontal: _inset),
            child: ThreadRow(
              key: ValueKey(thread.id),
              thread: thread,
              selected: chatShown && thread.id == state.currentThreadId,
              running: thread.id == state.runningThreadId,
              touch: touch,
              onTap: () => onOpenThread(thread),
              onDelete: state.threads.length <= 1
                  ? null
                  : () => onDeleteThread(thread),
            ),
          ));
        }
      }
    }
    return ListView(
      key: const PageStorageKey('shell-thread-list'),
      padding: const EdgeInsets.only(bottom: SonderSpace.md),
      children: children,
    );
  }
}

/// "Chats": the head of the conversation list and the Chat destination in
/// one row. Expanded, it titles the list and carries the project filter
/// when conversations span projects; on the rail it is the icon that
/// returns to the conversation, and shows as selected there (expanded, the
/// open conversation's own row carries the selection).
class _ChatsRow extends StatelessWidget {
  final bool selected;
  final SidebarBadge? badge;
  final String tooltip;
  final VoidCallback onTap;
  final List<String> projects;
  final String? projectFilter;
  final ValueChanged<String?> onProjectFilter;

  const _ChatsRow({
    required this.selected,
    required this.badge,
    required this.tooltip,
    required this.onTap,
    required this.projects,
    required this.projectFilter,
    required this.onProjectFilter,
  });

  @override
  Widget build(BuildContext context) {
    final e = _Expansion.of(context);
    final tokens = SonderTokens.of(context);
    final filter = projects.contains(projectFilter) ? projectFilter : null;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: _inset),
      child: Row(children: [
        Expanded(
          child: LayoutBuilder(
            builder: (context, box) => Align(
              alignment: Alignment.centerLeft,
              child: SizedBox(
                width: lerpDouble(_itemSize, box.maxWidth, e.t),
                child: KeyedSubtree(
                  key: Key(
                      'shell-destination-${WorkspaceDestination.chat.name}'),
                  child: _NavSurface(
                    icon: Icon(WorkspaceDestination.chat.icon,
                        size: 18,
                        color: selected ? tokens.text : tokens.text2),
                    label: 'Chats',
                    tooltip: tooltip,
                    onTap: onTap,
                    selected: selected,
                    badge: badge,
                    quietWhenExpanded: true,
                  ),
                ),
              ),
            ),
          ),
        ),
        if (projects.length > 1)
          _Fade(
            child: _ProjectFilter(
              projects: projects,
              selected: filter,
              onSelected: onProjectFilter,
            ),
          ),
      ]),
    );
  }
}

/// Narrows the conversation list to one project.
class _ProjectFilter extends StatelessWidget {
  final List<String> projects;
  final String? selected;
  final ValueChanged<String?> onSelected;

  const _ProjectFilter({
    required this.projects,
    required this.selected,
    required this.onSelected,
  });

  static const _all = '\u0000all';

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return PopupMenuButton<String>(
      key: const Key('shell-project-filter'),
      tooltip: 'Show chats from one project',
      popUpAnimationStyle: SonderMotion.menu(context),
      position: PopupMenuPosition.under,
      onSelected: (value) => onSelected(value == _all ? null : value),
      itemBuilder: (context) => [
        for (final value in [_all, ...projects])
          PopupMenuItem<String>(
            value: value,
            child: Row(children: [
              SizedBox(
                width: SonderSpace.xl,
                child: (value == _all ? selected == null : value == selected)
                    ? Icon(Icons.check, size: 16, color: tokens.accentText)
                    : null,
              ),
              const SizedBox(width: SonderSpace.sm),
              Flexible(
                child: Text(value == _all ? 'All projects' : value,
                    overflow: TextOverflow.ellipsis),
              ),
            ]),
          ),
      ],
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: _itemSize),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          const SizedBox(width: SonderSpace.sm),
          Icon(Icons.folder_outlined,
              size: 14,
              color: selected == null ? tokens.muted : tokens.accentText),
          const SizedBox(width: SonderSpace.xs),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 104),
            child: Text(selected ?? 'All projects',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: text.labelMedium?.copyWith(
                    color:
                        selected == null ? tokens.text2 : tokens.accentText)),
          ),
          Icon(Icons.expand_more, size: 16, color: tokens.muted),
          const SizedBox(width: SonderSpace.sm),
        ]),
      ),
    );
  }
}

class _Footer extends StatelessWidget {
  final ValueListenable<ConnectionStatus> connection;
  final VoidCallback onConnection;

  const _Footer({required this.connection, required this.onConnection});

  @override
  Widget build(BuildContext context) {
    final e = _Expansion.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(_inset, 0, _inset, SonderSpace.xs),
      child: Align(
        alignment: Alignment.centerLeft,
        child: SizedBox(
          width: e.itemWidth(),
          height: _itemSize,
          child: ValueListenableBuilder<ConnectionStatus>(
            valueListenable: connection,
            builder: (context, c, _) => Tooltip(
              message: _opensRuntime(c)
                  ? '${c.sentence} · open Runtime'
                  : '${c.sentence} · open connection settings',
              excludeFromSemantics: true,
              child: Semantics(
                container: true,
                child: HoverSurface(
                  key: const Key('shell-connection'),
                  onTap: onConnection,
                  inset: _pillInset,
                  child: ClipRect(
                    child: OverflowBox(
                      alignment: Alignment.centerLeft,
                      minWidth: e.fullWidth,
                      maxWidth: e.fullWidth,
                      child: Row(children: [
                        const SizedBox(width: SonderSpace.md),
                        Flexible(
                          child: ConnectionRow(
                            connection: connection,
                            glyphWidth: _slot,
                            gap: SonderSpace.md,
                            detailOpacity: _fadeOf(e.t),
                          ),
                        ),
                        const SizedBox(width: SonderSpace.md),
                      ]),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Whether the connection footer opens Runtime (the server answers, or is
/// being reached) rather than the connection settings.
bool _opensRuntime(ConnectionStatus c) =>
    c.isConnected || c.state == ConnState.connecting;
