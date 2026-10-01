import 'package:flutter/widgets.dart';

import '../../workspace_ui.dart';

/// Installed by the app shell around every destination page (Chat, Agents,
/// Runtime, Settings). A page that finds it does not draw its own way back
/// to Chat or a workspace menu: the shell's persistent sidebar (wide) or
/// drawer (narrow) already owns navigation. Without it (tests that pump a
/// screen alone, or a page pushed outside the shell) a page keeps its own
/// navigation chrome.
class ShellScope extends InheritedWidget {
  /// The destination currently shown.
  final WorkspaceDestination current;

  /// Whether a persistent sidebar is visible beside the page (wide layouts).
  /// When false the page shows a menu button that calls [openNavigation].
  final bool sidebarVisible;

  /// Switch the shell to [destination]; runs the shell's leave guards
  /// (unsaved settings, unsent agent drafts) first.
  final void Function(WorkspaceDestination destination) navigate;

  /// Open the navigation drawer (narrow layouts).
  final VoidCallback openNavigation;

  const ShellScope({
    super.key,
    required this.current,
    required this.sidebarVisible,
    required this.navigate,
    required this.openNavigation,
    required super.child,
  });

  static ShellScope? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<ShellScope>();

  @override
  bool updateShouldNotify(ShellScope oldWidget) =>
      current != oldWidget.current ||
      sidebarVisible != oldWidget.sidebarVisible;
}
