import 'package:flutter/material.dart';

import '../../workspace_ui.dart';

/// Installed by the app shell around every destination page (Chat, Agents,
/// Runtime, Settings). A page that finds it does not draw its own way back
/// to Chat or a workspace menu: the shell's persistent sidebar (wide) or
/// drawer (narrow) already owns navigation. Without it (tests that pump a
/// screen alone, or a page pushed outside the shell) a page keeps its own
/// navigation chrome.
///
/// A page that must ask before it is left (unsaved settings, unsent agent
/// drafts) wraps its content in a [ShellLeaveGuard]. A page that can open
/// at a section (a Settings or Runtime category, an agent lane) reads
/// [section].
class ShellScope extends InheritedWidget {
  /// The destination currently shown.
  final WorkspaceDestination current;

  /// Whether a persistent sidebar is visible beside the page (wide layouts).
  /// When false the page shows a menu button that calls [openNavigation]
  /// (see [ShellMenuButton]).
  final bool sidebarVisible;

  /// Switch the shell to [destination]; runs the shell's leave guards
  /// (unsaved settings, unsent agent drafts) first.
  final void Function(WorkspaceDestination destination) navigate;

  /// Open the navigation drawer (narrow layouts).
  final VoidCallback openNavigation;

  /// Switch to [destination] and ask it to show one section: a category id
  /// on Settings and Runtime (`connection`, `account`, `approvals`…), a lane
  /// id on Agents. Leave guards run first, as for [navigate]. Null in a
  /// shell without deep links.
  final void Function(WorkspaceDestination destination, String section,
      {Map<String, String> params})? openSection;

  /// The section this page was asked to show, or null. Each request is a new
  /// [ShellSection], so asking for the same section twice is two requests:
  /// a page compares with `identical` and applies each one once.
  final ShellSection? section;

  /// Where the [ShellLeaveGuard]s on this page register. Null outside a
  /// shell that runs guards; the guards are then inert.
  final ShellLeaveGuards? leaveGuards;

  const ShellScope({
    super.key,
    required this.current,
    required this.sidebarVisible,
    required this.navigate,
    required this.openNavigation,
    this.openSection,
    this.section,
    this.leaveGuards,
    required super.child,
  });

  static ShellScope? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<ShellScope>();

  @override
  bool updateShouldNotify(ShellScope oldWidget) =>
      current != oldWidget.current ||
      sidebarVisible != oldWidget.sidebarVisible ||
      !identical(section, oldWidget.section) ||
      !identical(leaveGuards, oldWidget.leaveGuards);
}

/// One request to show a section of a destination ([ShellScope.section]).
class ShellSection {
  /// A category id (`connection`, `account`, `approvals`…) or an agent lane
  /// id, depending on the destination.
  final String id;

  /// Extra values for the section, e.g. `username` for Settings > Account
  /// opened by a `/login` intercept. Never credentials.
  final Map<String, String> params;

  /// Not const on purpose: every request must be a distinct object.
  ShellSection(this.id, {this.params = const {}});

  @override
  String toString() => 'ShellSection($id)';
}

/// The leave guards registered on one destination page. The shell asks
/// [canLeave] before it switches away from that page.
class ShellLeaveGuards {
  final List<_ShellLeaveGuardState> _guards = <_ShellLeaveGuardState>[];

  /// Whether any guard is registered.
  bool get isEmpty => _guards.isEmpty;

  /// Runs the registered guards in order and stops at the first that wants
  /// to stay. True means the page may be left.
  Future<bool> canLeave() async {
    for (final guard in List.of(_guards)) {
      if (!guard.mounted) continue;
      if (!await guard.widget.canLeave()) return false;
    }
    return true;
  }

  void _add(_ShellLeaveGuardState guard) {
    if (!_guards.contains(guard)) _guards.add(guard);
  }

  void _remove(_ShellLeaveGuardState guard) => _guards.remove(guard);
}

/// Asks before the app shell leaves the page around it, the way [PopScope]
/// asks before a route pops: [canLeave] runs when the person picks another
/// destination (sidebar, drawer, shortcut, system back) and returns false to
/// stay, usually after asking ("Discard unsaved settings?").
///
/// Outside a shell (no [ShellScope], or one without guards) it does nothing,
/// so a page can always include it.
class ShellLeaveGuard extends StatefulWidget {
  /// True to let the shell leave; false to stay on the page.
  final Future<bool> Function() canLeave;
  final Widget child;

  const ShellLeaveGuard({
    super.key,
    required this.canLeave,
    required this.child,
  });

  @override
  State<ShellLeaveGuard> createState() => _ShellLeaveGuardState();
}

class _ShellLeaveGuardState extends State<ShellLeaveGuard> {
  ShellLeaveGuards? _registry;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final next = ShellScope.maybeOf(context)?.leaveGuards;
    if (identical(next, _registry)) return;
    _registry?._remove(this);
    _registry = next?.._add(this);
  }

  @override
  void dispose() {
    _registry?._remove(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}

/// The leading menu button a page shows on narrow layouts inside the app
/// shell: it opens the navigation drawer. It draws nothing when the sidebar
/// is visible or when there is no shell. Use [ShellMenuButton.shown] to
/// decide whether to pass it as an app bar's `leading`:
///
/// ```dart
/// leading: ShellMenuButton.shown(context) ? const ShellMenuButton() : null,
/// ```
class ShellMenuButton extends StatelessWidget {
  const ShellMenuButton({super.key});

  /// Whether a [ShellMenuButton] at [context] draws anything.
  static bool shown(BuildContext context) {
    final shell = ShellScope.maybeOf(context);
    return shell != null && !shell.sidebarVisible;
  }

  @override
  Widget build(BuildContext context) {
    final shell = ShellScope.maybeOf(context);
    if (shell == null || shell.sidebarVisible) return const SizedBox.shrink();
    return IconButton(
      key: const Key('shell-menu'),
      tooltip: 'Open navigation',
      icon: const Icon(Icons.menu),
      onPressed: shell.openNavigation,
    );
  }
}
