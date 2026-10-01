import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../chat/drawer.dart';
import '../models.dart';
import '../theme.dart';
import '../ui/sheet.dart';

/// Search the local conversations by title (or project) and open one: a
/// dialog on wide windows, a sheet on phones. Returns the chosen thread.
Future<ChatThread?> showThreadSwitcher(
  BuildContext context, {
  required List<ChatThread> threads,
  required String currentThreadId,
  required DateTime now,
}) {
  final height = math.min(560.0, MediaQuery.sizeOf(context).height * 0.8);
  return showSonderSheet<ChatThread>(
    context,
    builder: (context) => SizedBox(
      height: height,
      child: ThreadSwitcher(
        threads: threads,
        currentThreadId: currentThreadId,
        now: now,
      ),
    ),
  );
}

class ThreadSwitcher extends StatefulWidget {
  final List<ChatThread> threads;
  final String currentThreadId;
  final DateTime now;

  const ThreadSwitcher({
    super.key,
    required this.threads,
    required this.currentThreadId,
    required this.now,
  });

  @override
  State<ThreadSwitcher> createState() => _ThreadSwitcherState();
}

class _ThreadSwitcherState extends State<ThreadSwitcher> {
  final _query = TextEditingController();
  final _scroll = ScrollController();
  int _highlight = 0;

  @override
  void dispose() {
    _query.dispose();
    _scroll.dispose();
    super.dispose();
  }

  List<ChatThread> get _matches {
    final q = _query.text.trim().toLowerCase();
    if (q.isEmpty) return widget.threads;
    return [
      for (final t in widget.threads)
        if (t.displayTitle.toLowerCase().contains(q) ||
            t.project.toLowerCase().contains(q))
          t,
    ];
  }

  void _open(ChatThread thread) => Navigator.of(context).pop(thread);

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
      return KeyEventResult.ignored;
    }
    final matches = _matches;
    if (matches.isEmpty) return KeyEventResult.ignored;
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.arrowDown) {
      setState(() => _highlight = (_highlight + 1) % matches.length);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.arrowUp) {
      setState(() =>
          _highlight = (_highlight - 1 + matches.length) % matches.length);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.enter && event is KeyDownEvent) {
      _open(matches[_highlight.clamp(0, matches.length - 1)]);
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final matches = _matches;
    final searching = _query.text.trim().isNotEmpty;
    final highlight =
        matches.isEmpty ? -1 : _highlight.clamp(0, matches.length - 1);
    final projects = {for (final t in widget.threads) t.project}.length;

    final rows = <Widget>[];
    var index = 0;
    Widget row(ChatThread thread) {
      final i = index++;
      return _SwitcherRow(
        key: ValueKey('switcher-${thread.id}'),
        thread: thread,
        highlighted: i == highlight,
        current: thread.id == widget.currentThreadId,
        showProject: projects > 1,
        onTap: () => _open(thread),
      );
    }

    if (searching) {
      rows.addAll(matches.map(row));
    } else {
      for (final (age, threads) in groupThreads(matches, widget.now)) {
        rows.add(Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.xl, SonderSpace.md, SonderSpace.xl, SonderSpace.xs),
          child: Text(age.label.toUpperCase(), style: text.labelSmall),
        ));
        rows.addAll(threads.map(row));
      }
    }

    return Semantics(
      key: const Key('thread-switcher'),
      scopesRoute: true,
      namesRoute: true,
      explicitChildNodes: true,
      label: 'Search chats',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, SonderSpace.lg, SonderSpace.lg, SonderSpace.sm),
            child: Focus(
              onKeyEvent: _onKey,
              child: TextField(
                key: const Key('thread-switcher-search'),
                controller: _query,
                autofocus: true,
                textInputAction: TextInputAction.go,
                onChanged: (_) => setState(() => _highlight = 0),
                onSubmitted: (_) {
                  if (matches.isNotEmpty) _open(matches[highlight]);
                },
                decoration: InputDecoration(
                  hintText: 'Search chats',
                  prefixIcon: Icon(Icons.search, size: 18, color: tokens.muted),
                ),
              ),
            ),
          ),
          Expanded(
            child: matches.isEmpty
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(SonderSpace.xxl),
                      child: Text('No chats match "${_query.text.trim()}"',
                          textAlign: TextAlign.center,
                          style:
                              text.bodyMedium?.copyWith(color: tokens.muted)),
                    ),
                  )
                : ListView(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(
                        SonderSpace.sm, 0, SonderSpace.sm, SonderSpace.md),
                    children: rows,
                  ),
          ),
        ],
      ),
    );
  }
}

class _SwitcherRow extends StatelessWidget {
  final ChatThread thread;
  final bool highlighted;
  final bool current;
  final bool showProject;
  final VoidCallback onTap;

  const _SwitcherRow({
    super.key,
    required this.thread,
    required this.highlighted,
    required this.current,
    required this.showProject,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Semantics(
      selected: current,
      child: Material(
        type: MaterialType.transparency,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(SonderRadius.row),
          child: Container(
            constraints: const BoxConstraints(minHeight: 48),
            padding: const EdgeInsets.symmetric(horizontal: SonderSpace.md),
            decoration: BoxDecoration(
              color: highlighted ? tokens.raised : null,
              borderRadius: BorderRadius.circular(SonderRadius.row),
            ),
            child: Row(children: [
              Expanded(
                child: Text(
                  thread.displayTitle,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  softWrap: false,
                  style: text.bodyMedium?.copyWith(
                    color: tokens.text,
                    fontWeight: current ? FontWeight.w600 : FontWeight.w400,
                  ),
                ),
              ),
              if (showProject) ...[
                const SizedBox(width: SonderSpace.md),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 120),
                  child: Text(thread.project,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: text.bodySmall?.copyWith(color: tokens.muted)),
                ),
              ],
              if (current) ...[
                const SizedBox(width: SonderSpace.md),
                Text('current',
                    style: text.bodySmall?.copyWith(color: tokens.muted)),
              ],
            ]),
          ),
        ),
      ),
    );
  }
}
