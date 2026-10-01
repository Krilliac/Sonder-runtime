import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';

import '../models.dart';
import '../theme.dart';
import '../ui/kit.dart';
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

  const ThreadRow({
    super.key,
    required this.thread,
    required this.selected,
    required this.onTap,
    required this.onDelete,
    this.running = false,
    this.touch = false,
  });

  @override
  State<ThreadRow> createState() => _ThreadRowState();
}

class _ThreadRowState extends State<ThreadRow> {
  bool _hover = false;
  bool _focused = false;
  bool _revealed = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final onDelete = widget.onDelete;
    final showDelete = onDelete != null &&
        (_hover || _focused || _revealed || (widget.touch && widget.selected));
    final title = widget.thread.displayTitle;
    final background = widget.selected
        ? tokens.raised
        : _hover
            ? tokens.raised.withValues(alpha: 0.6)
            : Colors.transparent;
    return Semantics(
      container: true,
      selected: widget.selected,
      customSemanticsActions: onDelete == null
          ? null
          : {const CustomSemanticsAction(label: 'Delete chat'): onDelete},
      child: MouseRegion(
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: Focus(
          canRequestFocus: false,
          skipTraversal: true,
          onFocusChange: (focused) => setState(() => _focused = focused),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 1),
            child: Material(
              type: MaterialType.transparency,
              child: InkWell(
                onTap: widget.onTap,
                onLongPress: widget.touch && onDelete != null
                    ? () => setState(() => _revealed = true)
                    : null,
                borderRadius: BorderRadius.circular(SonderRadius.row),
                child: AnimatedContainer(
                  duration: SonderMotion.of(context, SonderMotion.fast),
                  curve: SonderMotion.standard,
                  constraints: const BoxConstraints(minHeight: 48),
                  padding: const EdgeInsets.only(left: SonderSpace.md),
                  decoration: BoxDecoration(
                    color: background,
                    borderRadius: BorderRadius.circular(SonderRadius.row),
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
                            color: widget.selected ? tokens.text : tokens.text2,
                            fontWeight: widget.selected
                                ? FontWeight.w500
                                : FontWeight.w400,
                          ),
                        ),
                      ),
                      if (showDelete)
                        IconButton(
                          tooltip: 'Delete chat',
                          onPressed: onDelete,
                          iconSize: 16,
                          icon: Icon(Icons.close, color: tokens.muted),
                        )
                      else
                        const SizedBox(width: SonderSpace.md),
                    ],
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
