import 'dart:async';

import 'package:flutter/material.dart';

/// Hosts one destination page in its own [Navigator].
///
/// * Routes the page pushes (Server conversations, Managed work) open inside
///   the content pane, with the sidebar still beside them.
/// * The page's root route never pops. A page that pops itself (an older
///   page's "back to chat", or system back on a destination other than
///   Chat) calls [onLeaveRequested] instead, and the shell decides where to
///   go, running its leave guards. Without this a pop would empty the app.
class ShellPageNavigator extends StatefulWidget {
  final Widget child;

  /// The page's navigator, for the shell's system-back handling.
  final GlobalKey<NavigatorState>? navigatorKey;

  /// The page asked to leave by popping its root route.
  final VoidCallback onLeaveRequested;

  /// Whether system back on the bare page means "leave this destination"
  /// (Agents, Runtime, Settings). For Chat it is false: back bubbles to the
  /// app, which closes it on Android.
  final bool leaveOnBack;

  /// Called (after the change, never during a build) when routes are pushed
  /// on top of the page or the last one is popped.
  final ValueChanged<bool>? onHasRoutesChanged;

  const ShellPageNavigator({
    super.key,
    required this.child,
    required this.onLeaveRequested,
    this.navigatorKey,
    this.leaveOnBack = true,
    this.onHasRoutesChanged,
  });

  @override
  State<ShellPageNavigator> createState() => _ShellPageNavigatorState();
}

class _ShellPageNavigatorState extends State<ShellPageNavigator> {
  late final _observer = _StackObserver(_stackChanged);
  bool _hasRoutes = false;

  void _stackChanged(NavigatorState navigator) {
    // Observers run while the navigator is busy; report afterwards.
    scheduleMicrotask(() {
      if (!mounted) return;
      final hasRoutes = navigator.canPop();
      if (hasRoutes == _hasRoutes) return;
      _hasRoutes = hasRoutes;
      widget.onHasRoutesChanged?.call(hasRoutes);
    });
  }

  void _leave() => scheduleMicrotask(() {
        if (mounted) widget.onLeaveRequested();
      });

  @override
  Widget build(BuildContext context) {
    // A semantics boundary: every page route brings a modal barrier whose
    // BlockSemantics drops whatever was painted before it in the same
    // container. Without this one, that would be the whole sidebar.
    return Semantics(
      container: true,
      child: Navigator(
        key: widget.navigatorKey,
        observers: [_observer],
        pages: [
          _RootPage(
            child: widget.child,
            leaveOnBack: widget.leaveOnBack,
            onLeaveRequested: _leave,
          ),
        ],
        // The root page is never removed (its route vetoes the pop), and
        // pushed routes are not pages.
        onDidRemovePage: (_) {},
      ),
    );
  }
}

class _StackObserver extends NavigatorObserver {
  final void Function(NavigatorState navigator) onChanged;
  _StackObserver(this.onChanged);

  void _changed() {
    final navigator = this.navigator;
    if (navigator != null) onChanged(navigator);
  }

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _changed();

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _changed();

  @override
  void didRemove(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _changed();

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) =>
      _changed();
}

class _RootPage extends Page<void> {
  final Widget child;
  final bool leaveOnBack;
  final VoidCallback onLeaveRequested;

  const _RootPage({
    required this.child,
    required this.leaveOnBack,
    required this.onLeaveRequested,
  }) : super(key: const ValueKey('shell-page-root'));

  @override
  Route<void> createRoute(BuildContext context) => _RootRoute(this);
}

/// The page itself: no transition of its own (the shell fades between
/// destinations), and a pop is turned into a request to leave.
class _RootRoute extends PageRoute<void> {
  _RootRoute(_RootPage page) : super(settings: page);

  _RootPage get _page => settings as _RootPage;

  @override
  Color? get barrierColor => null;

  @override
  String? get barrierLabel => null;

  @override
  bool get maintainState => true;

  @override
  bool get opaque => true;

  @override
  Duration get transitionDuration => Duration.zero;

  @override
  Duration get reverseTransitionDuration => Duration.zero;

  @override
  Widget buildPage(BuildContext context, Animation<double> animation,
          Animation<double> secondaryAnimation) =>
      _page.child;

  @override
  RoutePopDisposition get popDisposition {
    // PopScopes on the page and local history (an open drawer) still win.
    final base = super.popDisposition;
    if (base == RoutePopDisposition.bubble && _page.leaveOnBack) {
      return RoutePopDisposition.pop;
    }
    return base;
  }

  @override
  bool didPop(void result) {
    // Local history (a page's own drawer or search) pops as usual.
    if (willHandlePopInternally) return super.didPop(result);
    _page.onLeaveRequested();
    return false;
  }
}
