import 'dart:async' show unawaited;
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';

import '../models.dart';
import '../shell/chat_link.dart' show ChatShareKind;
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/staticy_painter.dart';
import '../ui/status_vocab.dart';
import 'connection.dart';

/// Colour for a connection state; always paired with its word.
Color connectionColor(SonderTokens tokens, ConnState state) => switch (state) {
      ConnState.connected => tokens.ok,
      ConnState.connecting => tokens.muted,
      ConnState.refused ||
      ConnState.unauthorized ||
      ConnState.serverError =>
        tokens.warn,
      ConnState.unreachable => tokens.danger,
    };

/// `✓ connected  127.0.0.1:11435` / `! refused  mypc.local` /
/// `✗ can't reach  …`: the glyph and word first, then the host. The
/// sentence ("Can't reach 127.0.0.1:11435") is what a screen reader hears.
class ConnectionRow extends StatelessWidget {
  final ValueListenable<ConnectionStatus> connection;

  /// Centre the glyph in a column this wide (the sidebar's icon column);
  /// null hugs the glyph.
  final double? glyphWidth;

  /// Space between the glyph column and the word.
  final double gap;

  /// How visible the word and host are: the collapsed rail fades them out
  /// and keeps the glyph.
  final double detailOpacity;

  const ConnectionRow({
    super.key,
    required this.connection,
    this.glyphWidth,
    this.gap = SonderSpace.sm,
    this.detailOpacity = 1,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<ConnectionStatus>(
      valueListenable: connection,
      builder: (context, c, _) {
        final tone = connectionColor(tokens, c.state);
        final glyph = Text(c.glyph,
            style: tokens.mono(12, color: tone, weight: FontWeight.w600));
        return Semantics(
          key: const Key('rail-connection'),
          label: c.sentence,
          child: ExcludeSemantics(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (glyphWidth == null)
                  glyph
                else
                  SizedBox(width: glyphWidth, child: Center(child: glyph)),
                SizedBox(width: gap),
                Flexible(
                  child: Opacity(
                    opacity: detailOpacity,
                    child: Row(mainAxisSize: MainAxisSize.min, children: [
                      Text(c.word,
                          style: tokens.mono(12,
                              color: tone, weight: FontWeight.w500)),
                      const SizedBox(width: SonderSpace.sm),
                      Flexible(
                        child: Text(c.host,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            softWrap: false,
                            style: tokens.mono(12, color: tokens.muted)),
                      ),
                    ]),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}

/// How long ago a conversation last changed, as the sidebar groups it.
enum ThreadAge {
  today('Today'),
  yesterday('Yesterday'),
  week('Previous 7 days'),
  older('Older');

  final String label;
  const ThreadAge(this.label);

  /// The group for a conversation last updated at [updated], seen at [now].
  /// Calendar days in local time: "Yesterday" is the day before today, not
  /// the last 24 hours.
  static ThreadAge of(DateTime updated, DateTime now) {
    final today = DateTime(now.year, now.month, now.day);
    final day = DateTime(updated.year, updated.month, updated.day);
    final days = today.difference(day).inDays;
    if (days <= 0) return ThreadAge.today;
    if (days == 1) return ThreadAge.yesterday;
    if (days <= 7) return ThreadAge.week;
    return ThreadAge.older;
  }
}

/// [threads] (newest first) in date groups, in [ThreadAge] order, without
/// empty groups. Order inside a group is kept.
List<(ThreadAge, List<ChatThread>)> groupThreads(
    List<ChatThread> threads, DateTime now) {
  final groups = <ThreadAge, List<ChatThread>>{};
  for (final thread in threads) {
    groups
        .putIfAbsent(ThreadAge.of(thread.updatedAt, now), () => [])
        .add(thread);
  }
  return [
    for (final age in ThreadAge.values)
      if (groups[age] case final list?) (age, list),
  ];
}

/// One conversation in the sidebar: its title on one line, a working mark
/// while its turn streams, and Delete revealed on hover or keyboard focus.
/// On touch ([touch]) there is no hover: the open conversation shows
/// Delete, and a long press reveals it on any other. Screen readers get
/// Delete as a custom action on the row.
class ThreadRow extends StatefulWidget {
  final ChatThread thread;
  final bool selected;

  /// The conversation's turn is streaming (it may not be the open one).
  final bool running;
  final VoidCallback onTap;
  final VoidCallback? onDelete;

  /// A touch layout (the navigation drawer) rather than a pointer one.
  final bool touch;

  /// Copy this chat's link or code. When set, the row has a context menu
  /// (right click, long press, or the "..." button shown on hover and focus)
  /// with Copy link and Copy chat code; Delete chat joins it when allowed.
  final ValueChanged<ChatShareKind>? onShare;

  const ThreadRow({
    super.key,
    required this.thread,
    required this.selected,
    required this.onTap,
    required this.onDelete,
    this.running = false,
    this.touch = false,
    this.onShare,
  });

  @override
  State<ThreadRow> createState() => _ThreadRowState();
}

enum _RowAction { copyLink, copyCode, delete }

class _ThreadRowState extends State<ThreadRow> with SingleTickerProviderStateMixin {
  bool _hover = false;
  bool _focused = false;
  bool _ring = false;
  bool _revealed = false;
  bool _menuOpen = false;
  late AnimationController _particleController;

  @override
  void initState() {
    super.initState();
    _particleController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 400),
    );
  }

  @override
  void dispose() {
    _particleController.dispose();
    super.dispose();
  }

  /// The row's hit area; the pill drawn inside it is inset by [_pillInset].
  static const double _rowHeight = SonderSpace.x5;
  static const double _pillInset = SonderSpace.xs;

  List<PopupMenuEntry<_RowAction>> _menuEntries() => [
        if (widget.onShare != null) ...const [
          PopupMenuItem<_RowAction>(
              key: Key('thread-menu-copy-link'),
              value: _RowAction.copyLink,
              child: Text('Copy link')),
          PopupMenuItem<_RowAction>(
              key: Key('thread-menu-copy-code'),
              value: _RowAction.copyCode,
              child: Text('Copy chat code')),
        ],
        if (widget.onDelete != null)
          const PopupMenuItem<_RowAction>(
              key: Key('thread-menu-delete'),
              value: _RowAction.delete,
              child: Text('Delete chat')),
      ];

  void _apply(_RowAction? action) {
    switch (action) {
      case _RowAction.copyLink:
        widget.onShare?.call(ChatShareKind.link);
      case _RowAction.copyCode:
        widget.onShare?.call(ChatShareKind.code);
      case _RowAction.delete:
        widget.onDelete?.call();
      case null:
        break;
    }
  }

  /// The context menu at [globalPosition] (the pointer, or the row's middle).
  Future<void> _openMenuAt(Offset globalPosition) async {
    if (widget.onShare == null || _menuOpen) return;
    final overlay = Overlay.of(context).context.findRenderObject() as RenderBox;
    // The row's actions stay drawn while its menu is open, so moving the
    // pointer onto the menu cannot take the "..." button away.
    setState(() => _menuOpen = true);
    final action = await showMenu<_RowAction>(
      context: context,
      position: RelativeRect.fromRect(
        Rect.fromLTWH(globalPosition.dx, globalPosition.dy, 0, 0),
        Offset.zero & overlay.size,
      ),
      items: _menuEntries(),
      popUpAnimationStyle: SonderMotion.menu(context),
    );
    if (!mounted) return;
    setState(() => _menuOpen = false);
    _apply(action);
  }

  /// Screen-reader equivalents of the row's menu items.
  Map<CustomSemanticsAction, VoidCallback>? actionsFor(
      VoidCallback? onDelete, ValueChanged<ChatShareKind>? onShare) {
    if (onDelete == null && onShare == null) return null;
    return {
      if (onShare != null) ...{
        const CustomSemanticsAction(label: 'Copy link'): () =>
            onShare(ChatShareKind.link),
        const CustomSemanticsAction(label: 'Copy chat code'): () =>
            onShare(ChatShareKind.code),
      },
      if (onDelete != null)
        const CustomSemanticsAction(label: 'Delete chat'): onDelete,
    };
  }

  void _openMenuAtRow() {
    final box = context.findRenderObject() as RenderBox?;
    if (box == null || !box.attached) return;
    _openMenuAt(box.localToGlobal(box.size.center(Offset.zero)));
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final onDelete = widget.onDelete;
    final onShare = widget.onShare;
    final showDelete = onDelete != null &&
        (_hover || _focused || _revealed || (widget.touch && widget.selected));
    // Touch has no hover, so the menu button is always there.
    final showMore = onShare != null &&
        (_hover || _focused || _revealed || _menuOpen || widget.touch);
    final actionCount = (showDelete ? 1 : 0) + (showMore ? 1 : 0);
    final title = widget.thread.displayTitle;
    final background = widget.selected
        ? tokens.raised
        : _hover
            ? tokens.raised.withValues(alpha: 0.6)
            : Colors.transparent;
    final radius = BorderRadius.circular(SonderRadius.row);
    return Semantics(
      container: true,
      selected: widget.selected,
      customSemanticsActions: actionsFor(onDelete, onShare),
      child: MouseRegion(
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: Focus(
          canRequestFocus: false,
          skipTraversal: true,
          onFocusChange: (focused) => setState(() => _focused = focused),
          // Delete is a full 48 px target laid over the row, so the pill
          // can stay slim without shrinking what a finger has to hit.
          child: Stack(children: [
            Material(
              type: MaterialType.transparency,
              child: InkWell(
                onTap: () {
                  _particleController.forward(from: 0);
                  widget.onTap();
                },
                onSecondaryTapDown: onShare == null
                    ? null
                    : (details) => _openMenuAt(details.globalPosition),
                onLongPress: onShare != null
                    ? _openMenuAtRow
                    : widget.touch && onDelete != null
                        ? () => setState(() => _revealed = true)
                        : null,
                borderRadius: radius,
                // The pill is the hover, press and focus feedback.
                overlayColor: const WidgetStatePropertyAll(Colors.transparent),
                onFocusChange: (focused) {
                  final keyboard = FocusManager.instance.highlightMode ==
                      FocusHighlightMode.traditional;
                  final show = focused && keyboard;
                  if (show != _ring) setState(() => _ring = show);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: _pillInset),
                  child: AnimatedContainer(
                    duration: SonderMotion.of(context, SonderMotion.fast),
                    curve: SonderMotion.standard,
                    constraints: const BoxConstraints(
                        minHeight: _rowHeight - 2 * _pillInset),
                    padding: EdgeInsets.only(
                        left: SonderSpace.md,
                        right: actionCount == 0
                            ? SonderSpace.md
                            : SonderSpace.x4 * actionCount +
                                (showMore && showDelete ? SonderSpace.sm : 0)),
                    decoration:
                        BoxDecoration(color: background, borderRadius: radius),
                    foregroundDecoration: BoxDecoration(
                      borderRadius: radius,
                      border: Border.all(
                        color: _ring ? tokens.accentText : Colors.transparent,
                        width: 1.5,
                      ),
                    ),
                    child: Row(
                      children: [
                        if (widget.running) ...[
                          Tooltip(
                            message: 'Working',
                            excludeFromSemantics: true,
                            child: Semantics(
                              label: StatusKind.running.word,
                              child: Text(StatusKind.running.glyph,
                                  style: tokens.mono(12,
                                      color: tokens.accentText,
                                      weight: FontWeight.w600)),
                            ),
                          ),
                          const SizedBox(width: SonderSpace.sm),
                        ],
                        Expanded(
                          child: Text(
                            title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            softWrap: false,
                            style: text.bodyMedium?.copyWith(
                              color:
                                  widget.selected ? tokens.text : tokens.text2,
                              fontWeight: widget.selected
                                  ? FontWeight.w500
                                  : FontWeight.w400,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
            IgnorePointer(
              child: AnimatedBuilder(
                animation: _particleController,
                builder: (context, _) => Positioned.fill(
                  child: CustomPaint(
                      painter: StaticyPainter(_particleController.value)),
                ),
              ),
            ),
            if (actionCount > 0)
              Positioned(
                right: 0,
                top: 0,
                bottom: 0,
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  if (showMore)
                    Builder(
                      builder: (buttonContext) => IconButton(
                        key: const Key('thread-menu-button'),
                        tooltip: 'Chat actions',
                        iconSize: 16,
                        icon: Icon(Icons.more_horiz, color: tokens.muted),
                        onPressed: () {
                          final box =
                              buttonContext.findRenderObject() as RenderBox;
                          unawaited(_openMenuAt(box.localToGlobal(
                              box.size.bottomLeft(Offset.zero))));
                        },
                      ),
                    ),
                  if (showMore && showDelete)
                    const SizedBox(width: SonderSpace.sm),
                  if (showDelete)
                    IconButton(
                      tooltip: 'Delete chat',
                      onPressed: onDelete,
                      iconSize: 16,
                      icon: Icon(Icons.close, color: tokens.muted),
                    ),
                ]),
              ),
          ]),
        ),
      ),
    );
  }
}

/// Placeholder rows shaped like conversation titles, while history loads.
class ThreadRowsSkeleton extends StatelessWidget {
  final int rows;
  const ThreadRowsSkeleton({super.key, this.rows = 5});

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Loading chats',
      liveRegion: true,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (var i = 0; i < rows; i++)
            Padding(
              padding: const EdgeInsets.symmetric(
                  horizontal: SonderSpace.md, vertical: SonderSpace.lg),
              child: Skeleton(width: 120.0 + (i * 37) % 90, height: 12),
            ),
        ],
      ),
    );
  }
}
